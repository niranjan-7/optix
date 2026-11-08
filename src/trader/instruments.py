"""Angel One instrument master: download, cache, and option-lookup helpers.

Why the care around downloading: the file is ~38 MB and Angel One's server
sometimes cuts the connection early (HTTP 200 but truncated). We therefore
(1) compare bytes received to Content-Length, (2) check it parses as JSON,
(3) write atomically, and (4) fall back to the newest cached copy rather than
leave the system with no tokens. A stale master is safer than none, but we
warn loudly because expiries/lot sizes can change.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# NOTE: path is OpenAPI_File (singular). Not officially documented; if Angel One
# moves it, only this constant needs to change.
MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MIN_PLAUSIBLE_ROWS = 1000

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


class InstrumentError(RuntimeError):
    """Operator-friendly problem with the instrument master or a lookup."""


@dataclass(frozen=True)
class Option:
    token: str
    symbol: str
    name: str
    expiry: date
    strike: float
    opt_type: str  # "CE" or "PE"
    lot_size: int
    tick_size: float
    freeze_qty: int


@dataclass(frozen=True)
class Future:
    token: str
    symbol: str
    name: str
    expiry: date
    lot_size: int


def parse_expiry(s: str) -> date:
    """'09NOV2026' -> date. Manual parse: strptime('%b') depends on locale."""
    try:
        return date(int(s[5:]), _MONTHS[s[2:5].upper()], int(s[:2]))
    except (KeyError, ValueError) as e:
        raise InstrumentError(f"Unrecognised expiry format: {s!r}") from e


# ---------------------------------------------------------------- download

def _http_fetch(url: str, dest: Path, timeout: float = 120) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as f:
        expected = r.headers.get("Content-Length")
        got = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            got += len(chunk)
    if expected is not None and got != int(expected):
        raise InstrumentError(f"Truncated download: got {got} of {expected} bytes")


def _validate(path: Path) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)  # raises on truncated JSON
    if not isinstance(data, list) or len(data) < MIN_PLAUSIBLE_ROWS:
        raise InstrumentError("Instrument file parsed but looks too small / wrong shape")


def _cache_path(cache_dir: Path, day: date) -> Path:
    return cache_dir / f"OpenAPIScripMaster-{day.isoformat()}.json"


def ensure_master(
    cache_dir: str | Path = "data/instruments",
    today: date | None = None,
    url: str = MASTER_URL,
    fetch: Callable[[str, Path], None] = _http_fetch,
) -> Path:
    """Return path to today's master, downloading it if needed.

    Falls back to the newest older cached file if the download fails.
    """
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    today = today or date.today()
    target = _cache_path(cache, today)
    if target.exists():
        return target

    tmp = target.with_suffix(".part")
    try:
        fetch(url, tmp)
        _validate(tmp)
        os.replace(tmp, target)  # atomic: never a half-written "today" file
        log.info("Downloaded instrument master to %s", target)
        return target
    except Exception as e:
        tmp.unlink(missing_ok=True)
        older = sorted(cache.glob("OpenAPIScripMaster-*.json"))
        if older:
            log.warning("Instrument download failed (%s); using STALE cache %s", e, older[-1].name)
            return older[-1]
        raise InstrumentError(
            f"Could not download the instrument master and no cached copy exists ({e}). "
            "Check internet connection; the URL may also have changed."
        ) from e


# ------------------------------------------------------------------ lookup

class InstrumentMaster:
    def __init__(self, options: list[Option], futures: list[Future], indices: dict[str, str] | None = None):
        self._options = options
        self._futures = futures
        self._indices = indices or {}  # name -> NSE spot-index token
        self._by_key: dict[tuple[str, date, float, str], Option] = {
            (o.name, o.expiry, o.strike, o.opt_type): o for o in options
        }

    # -- construction
    @classmethod
    def from_rows(cls, rows: list[dict], names: set[str] | None = None) -> "InstrumentMaster":
        options: list[Option] = []
        futures: list[Future] = []
        indices: dict[str, str] = {}
        for r in rows:
            if r.get("exch_seg") == "NSE" and r.get("instrumenttype") == "AMXIDX":
                if not names or r.get("name") in names:
                    indices[r["name"]] = r["token"]
                continue
            if r.get("exch_seg") != "NFO" or r.get("instrumenttype") not in ("OPTIDX", "FUTIDX"):
                continue
            if names and r.get("name") not in names:
                continue
            exp = parse_expiry(r["expiry"])
            if r["instrumenttype"] == "FUTIDX":
                futures.append(Future(r["token"], r["symbol"], r["name"], exp, int(r["lotsize"])))
                continue
            opt_type = r["symbol"][-2:]
            if opt_type not in ("CE", "PE"):
                continue
            options.append(Option(
                token=r["token"], symbol=r["symbol"], name=r["name"], expiry=exp,
                strike=float(r["strike"]) / 100.0,  # file stores strike x100
                opt_type=opt_type, lot_size=int(r["lotsize"]),
                tick_size=float(r["tick_size"]) / 100.0,  # also x100 (5.0 -> 0.05)
                freeze_qty=int(r.get("freeze_qty") or 0),
            ))
        if not options:
            raise InstrumentError("No index options found in the instrument file")
        return cls(options, futures, indices)

    @classmethod
    def load(cls, path: str | Path, names: set[str] | None = None) -> "InstrumentMaster":
        with open(path, encoding="utf-8") as f:
            return cls.from_rows(json.load(f), names)

    # -- queries
    def expiries(self, name: str, on_or_after: date | None = None) -> list[date]:
        ex = sorted({o.expiry for o in self._options if o.name == name})
        return [e for e in ex if on_or_after is None or e >= on_or_after]

    def nearest_expiry(self, name: str, today: date, skip_today: bool = False) -> date:
        """Nearest expiry; skip_today=True avoids expiry-day gamma if you prefer."""
        cutoff = today if not skip_today else date.fromordinal(today.toordinal() + 1)
        ex = self.expiries(name, cutoff)
        if not ex:
            raise InstrumentError(f"No upcoming {name} expiry found")
        return ex[0]

    def strikes(self, name: str, expiry: date) -> list[float]:
        s = sorted({o.strike for o in self._options if o.name == name and o.expiry == expiry})
        if not s:
            raise InstrumentError(f"No strikes for {name} expiring {expiry}")
        return s

    def find_option(self, name: str, expiry: date, strike: float, opt_type: str) -> Option:
        try:
            return self._by_key[(name, expiry, float(strike), opt_type.upper())]
        except KeyError:
            raise InstrumentError(f"No such option: {name} {expiry} {strike:g} {opt_type}") from None

    def atm_strike(self, name: str, expiry: date, underlying_price: float) -> float:
        """Listed strike closest to the underlying (ties go to the lower strike)."""
        strikes = self.strikes(name, expiry)
        return min(strikes, key=lambda k: (abs(k - underlying_price), k))

    def strikes_around_atm(self, name: str, expiry: date, underlying_price: float, n: int) -> list[float]:
        """ATM +/- n *listed* strikes (not n * 50), so gaps in the chain are handled."""
        strikes = self.strikes(name, expiry)
        atm = self.atm_strike(name, expiry, underlying_price)
        i = strikes.index(atm)
        return strikes[max(0, i - n): i + n + 1]

    def chain(self, name: str, expiry: date, underlying_price: float, n: int) -> list[Option]:
        """All CE and PE contracts within ATM +/- n strikes, sorted by strike."""
        ks = self.strikes_around_atm(name, expiry, underlying_price, n)
        return [self.find_option(name, expiry, k, t) for k in ks for t in ("CE", "PE")]

    def lot_size(self, name: str, expiry: date) -> int:
        for o in self._options:
            if o.name == name and o.expiry == expiry:
                return o.lot_size
        raise InstrumentError(f"Unknown {name} expiry {expiry}")

    def describe(self, exchange: str, token: str) -> dict | None:
        """A compact, self-contained description of a token, for archiving next to recorded prices."""
        if exchange == "NSE":
            for name, tok in self._indices.items():
                if tok == token:
                    return {"exchange": "NSE", "token": token, "kind": "IDX", "name": name, "symbol": name}
            return None
        if not hasattr(self, "_by_token"):
            self._by_token = {o.token: o for o in self._options} | {f.token: f for f in self._futures}
        c = self._by_token.get(token)
        if c is None:
            return None
        if isinstance(c, Option):
            return {"exchange": "NFO", "token": token, "kind": "OPT", "name": c.name, "symbol": c.symbol,
                    "expiry": c.expiry.isoformat(), "strike": c.strike, "opt_type": c.opt_type,
                    "lot_size": c.lot_size, "tick_size": c.tick_size}
        return {"exchange": "NFO", "token": token, "kind": "FUT", "name": c.name, "symbol": c.symbol,
                "expiry": c.expiry.isoformat(), "lot_size": c.lot_size}

    @classmethod
    def from_archive(cls, rows: list[dict]) -> "InstrumentMaster":
        """Rebuild a master from archived descriptions (so old, expired contracts can be replayed)."""
        options, futures, indices = [], [], {}
        for r in rows:
            if r["kind"] == "OPT":
                options.append(Option(r["token"], r["symbol"], r["name"], date.fromisoformat(r["expiry"]), float(r["strike"]),
                                      r["opt_type"], int(r["lot_size"]), float(r["tick_size"]), 0))
            elif r["kind"] == "FUT":
                futures.append(Future(r["token"], r["symbol"], r["name"], date.fromisoformat(r["expiry"]), int(r["lot_size"])))
            elif r["kind"] == "IDX":
                indices[r["name"]] = r["token"]
        if not options:
            raise InstrumentError("The contract archive contains no options")
        return cls(options, futures, indices)

    def index_token(self, name: str) -> str:
        """NSE cash-segment token of the spot index (e.g. NIFTY -> 'Nifty 50')."""
        try:
            return self._indices[name]
        except KeyError:
            raise InstrumentError(f"No spot index token for {name}") from None

    def nearest_future(self, name: str, today: date) -> Future:
        fut = sorted((f for f in self._futures if f.name == name and f.expiry >= today), key=lambda f: f.expiry)
        if not fut:
            raise InstrumentError(f"No {name} future found")
        return fut[0]


if __name__ == "__main__":
    # Quick manual check: python -m trader.instruments [spot_price]
    import sys
    from .logging_utils import setup_logging

    setup_logging()
    spot = float(sys.argv[1]) if len(sys.argv) > 1 else 25000.0
    master = InstrumentMaster.load(ensure_master(), {"NIFTY"})
    today = date.today()
    exp = master.nearest_expiry("NIFTY", today)
    print(f"Nearest NIFTY expiry: {exp}  lot size: {master.lot_size('NIFTY', exp)}")
    print(f"ATM for spot {spot:g}: {master.atm_strike('NIFTY', exp, spot):g}")
    print(f"Nearest future: {master.nearest_future('NIFTY', today).symbol}")
    for o in master.chain("NIFTY", exp, spot, 2):
        print(f"  {o.symbol:<26} token={o.token}")
