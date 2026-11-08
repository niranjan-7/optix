"""Positions and P&L, built from fills.

One class serves two readers that need slightly different views:
  * the risk engine  : open_positions(), realised_pnl_today(), unrealised_pnl()
  * the strategies   : open_positions(strategy), entries_today(strategy, day)

Positions are updated fill by fill (not once an order completes), so the risk
engine, kill switch and square-off always see what we really hold, even in the
middle of a partially filled order. State is written to disk after every change
so a restart does not forget open positions.

P&L conventions: realised P&L is booked when a position (or part) is closed and
includes estimated fees on EVERY fill. Unrealised P&L marks longs at the bid and
shorts at the ask (what it would cost to close right now).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from .feed import Tick
from .greeks import IST
from .signals import Leg, PositionView

log = logging.getLogger(__name__)


class FeeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brokerage_per_order: float = Field(20.0, ge=0)
    pct_of_turnover: float = Field(0.0, ge=0)

    def fee(self, qty: int, price: float, first_fill_of_order: bool) -> float:
        f = self.pct_of_turnover / 100.0 * qty * price
        if first_fill_of_order:  # brokerage is per order, not per partial fill
            f += self.brokerage_per_order
        return f


@dataclass
class _Held:
    leg: Leg  # template: contract + side (BUY = long, SELL = short); its .lots is the ORIGINAL size
    lots: int  # lots currently held (Leg itself refuses 0, so the live count lives here)
    avg_price: float  # average entry price per unit
    last_mark: float | None = None

    @property
    def qty(self) -> int:
        return self.lots * self.leg.lot_size


@dataclass
class _Position:
    position_id: str
    strategy: str
    stop_loss: float | None
    target: float | None
    opened_at: datetime
    held: list[_Held] = field(default_factory=list)


@dataclass(frozen=True)
class ClosedTrade:
    """Handed to the journal when a position is fully closed."""
    position_id: str
    strategy: str
    opened_at: datetime
    closed_at: datetime
    realised_pnl: float  # gross of this position's lifetime fills, net of fees
    fees: float


class Portfolio:
    def __init__(self, quotes: Callable[[str], Tick | None], fees: FeeModel | None = None,
                 state_file: str | Path | None = None, clock: Callable[[], datetime] | None = None):
        self._quotes = quotes
        self._fees = fees or FeeModel()
        self._path = Path(state_file) if state_file else None
        self._clock = clock or (lambda: datetime.now(IST))
        self._lock = threading.RLock()
        self._positions: dict[str, _Position] = {}
        self._realised: dict[str, float] = {}  # ISO day -> realised P&L
        self._entries: dict[str, int] = {}  # "strategy|day" -> entries
        self._lifetime: dict[str, tuple[float, float]] = {}  # position id -> (pnl, fees)
        self._counter = 0
        self.closed_trades: list[ClosedTrade] = []
        self._load()

    # ------------------------------------------------------------ reading
    def _view(self, p: _Position) -> PositionView:
        legs = tuple(dataclasses.replace(h.leg, lots=h.lots) for h in p.held if h.lots > 0)
        net = sum((h.avg_price if h.leg.side == "BUY" else -h.avg_price) for h in p.held if h.lots > 0)
        return PositionView(p.position_id, p.strategy, legs, net, p.stop_loss, p.target, p.opened_at)

    def open_positions(self, strategy: str | None = None) -> list[PositionView]:
        with self._lock:
            return [self._view(p) for p in self._positions.values()
                    if (strategy is None or p.strategy == strategy) and any(h.lots > 0 for h in p.held)]

    def entries_today(self, strategy: str, day: date) -> int:
        with self._lock:
            return self._entries.get(f"{strategy}|{day.isoformat()}", 0)

    def realised_pnl_today(self) -> float:
        with self._lock:
            return self._realised.get(self._clock().astimezone(IST).date().isoformat(), 0.0)

    def _mark(self, h: "_Held") -> float:
        """Price at which this leg could be closed right now (falls back to the last known, then entry)."""
        q = self._quotes(h.leg.token)
        mark = None
        if q is not None:
            # What it would take to close now: longs at the bid, shorts at the ask.
            mark = (q.bid if h.leg.side == "BUY" else q.ask) or q.ltp
        if mark is None:
            mark = h.last_mark if h.last_mark is not None else h.avg_price
        h.last_mark = mark
        return mark

    @staticmethod
    def _leg_pnl(h: "_Held", mark: float) -> float:
        return (mark - h.avg_price) * h.qty if h.leg.side == "BUY" else (h.avg_price - mark) * h.qty

    def unrealised_pnl(self) -> float:
        with self._lock:
            return sum(self._leg_pnl(h, self._mark(h))
                       for p in self._positions.values() for h in p.held if h.lots > 0)

    def positions_detail(self) -> list[dict]:
        """Open positions with per-leg entry, mark and unrealised P&L (for the dashboard)."""
        with self._lock:
            out = []
            for p in self._positions.values():
                legs, total = [], 0.0
                for h in p.held:
                    if h.lots <= 0:
                        continue
                    mark = self._mark(h)
                    pnl = self._leg_pnl(h, mark)
                    total += pnl
                    legs.append({"symbol": h.leg.symbol, "side": h.leg.side, "lots": h.lots, "entry": h.avg_price,
                                 "mark": mark, "pnl": pnl})
                if legs:
                    out.append({"position_id": p.position_id, "strategy": p.strategy, "stop_loss": p.stop_loss,
                                "target": p.target, "opened_at": p.opened_at.isoformat(), "legs": legs, "unrealised": total})
            return out

    def net_units_by_token(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for p in self._positions.values():
                for h in p.held:
                    if h.lots > 0:
                        out[h.leg.token] = out.get(h.leg.token, 0) + (h.qty if h.leg.side == "BUY" else -h.qty)
            return {k: v for k, v in out.items() if v}

    # ----------------------------------------------------------- mutating
    def _day(self) -> str:
        return self._clock().astimezone(IST).date().isoformat()

    def _book(self, amount: float) -> None:
        d = self._day()
        self._realised[d] = self._realised.get(d, 0.0) + amount

    def record_entry_fill(self, position_id: str | None, strategy: str, stop_loss: float | None,
                          target: float | None, leg: Leg, lots: int, price: float,
                          first_fill_of_order: bool) -> tuple[str, float]:
        """Add `lots` of `leg` at `price`. Creates the position on first fill. Returns (position_id, fee)."""
        if lots < 1 or price <= 0:
            raise ValueError("fill must have positive lots and price")
        with self._lock:
            now = self._clock()
            if position_id is None or position_id not in self._positions:
                self._counter += 1
                position_id = f"P{self._counter}"
                self._positions[position_id] = _Position(position_id, strategy, stop_loss, target, now)
                key = f"{strategy}|{self._day()}"
                self._entries[key] = self._entries.get(key, 0) + 1
            pos = self._positions[position_id]
            held = next((h for h in pos.held if h.leg.token == leg.token and h.leg.side == leg.side), None)
            if held is None:
                pos.held.append(_Held(leg, lots, price))
            else:
                total = held.lots + lots
                held.avg_price = (held.avg_price * held.lots + price * lots) / total
                held.lots = total
            fee = self._fees.fee(lots * leg.lot_size, price, first_fill_of_order)
            self._book(-fee)
            life_pnl, life_fee = self._lifetime.get(position_id, (0.0, 0.0))
            self._lifetime[position_id] = (life_pnl - fee, life_fee + fee)  # entry fees are part of this trade's cost
            self._save()
            return position_id, fee

    def record_exit_fill(self, position_id: str, closing_leg: Leg, lots: int, price: float,
                         first_fill_of_order: bool) -> tuple[float, float, ClosedTrade | None]:
        """Reduce the position by `lots` using a fill on the OPPOSITE side. Returns (pnl, fee, closed_trade)."""
        if lots < 1 or price <= 0:
            raise ValueError("fill must have positive lots and price")
        with self._lock:
            pos = self._positions.get(position_id)
            if pos is None:
                raise KeyError(f"no such position {position_id}")
            held_side = "SELL" if closing_leg.side == "BUY" else "BUY"
            held = next((h for h in pos.held if h.leg.token == closing_leg.token and h.leg.side == held_side
                         and h.lots > 0), None)
            if held is None or lots > held.lots:
                raise ValueError(f"fill of {lots} x {closing_leg.symbol} does not match what position {position_id} holds")
            qty = lots * closing_leg.lot_size
            pnl = (price - held.avg_price) * qty if held.leg.side == "BUY" else (held.avg_price - price) * qty
            fee = self._fees.fee(qty, price, first_fill_of_order)
            held.lots -= lots
            self._book(pnl - fee)
            life_pnl, life_fee = self._lifetime.get(position_id, (0.0, 0.0))
            self._lifetime[position_id] = (life_pnl + pnl - fee, life_fee + fee)
            closed = None
            if all(h.lots == 0 for h in pos.held):
                closed = ClosedTrade(position_id, pos.strategy, pos.opened_at, self._clock(),
                                     self._lifetime[position_id][0], self._lifetime[position_id][1])
                self.closed_trades.append(closed)
                del self._positions[position_id]
                self._lifetime.pop(position_id, None)
            self._save()
            return pnl, fee, closed

    # -------------------------------------------------------- persistence
    def _save(self) -> None:
        if self._path is None:
            return
        data = {
            "counter": self._counter, "realised": self._realised, "entries": self._entries,
            "lifetime": {k: list(v) for k, v in self._lifetime.items()},
            "positions": [{
                "position_id": p.position_id, "strategy": p.strategy, "stop_loss": p.stop_loss, "target": p.target,
                "opened_at": p.opened_at.isoformat(),
                "held": [{"leg": {**dataclasses.asdict(h.leg), "expiry": h.leg.expiry.isoformat()},
                          "lots": h.lots, "avg_price": h.avg_price} for h in p.held],
            } for p in self._positions.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._counter = int(data["counter"])
            self._realised = {k: float(v) for k, v in data["realised"].items()}
            self._entries = {k: int(v) for k, v in data["entries"].items()}
            self._lifetime = {k: (float(v[0]), float(v[1])) for k, v in data.get("lifetime", {}).items()}
            for pd in data["positions"]:
                pos = _Position(pd["position_id"], pd["strategy"], pd["stop_loss"], pd["target"],
                                datetime.fromisoformat(pd["opened_at"]))
                for hd in pd["held"]:
                    ld = dict(hd["leg"]); ld["expiry"] = date.fromisoformat(ld["expiry"])
                    pos.held.append(_Held(Leg(**ld), int(hd["lots"]), float(hd["avg_price"])))
                self._positions[pos.position_id] = pos
            log.info("Portfolio restored: %d open position(s)", len(self._positions))
        except Exception as e:
            # Do NOT start with an empty book and carry on: positions may exist. Keep the
            # evidence and stop, so a person checks the broker before anything trades.
            backup = self._path.with_name(f"{self._path.name}.corrupt.{int(time.time())}")
            os.replace(self._path, backup)
            raise RuntimeError(
                f"Portfolio file was unreadable ({type(e).__name__}: {e}). Saved as {backup.name}. "
                "Check your positions at the broker and repair or remove the file before restarting.") from e
