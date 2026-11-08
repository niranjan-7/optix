"""Chain snapshots for later backtesting, kept apart from the trade journal.

Why a separate file: market data grows every day and can be pruned or thrown away;
the trade journal is small and precious and should be backed up. Keeping them in
different SQLite files means pruning one can never damage the other.

What is stored: at the first tick after every `interval_seconds` has passed, the latest quote (last price, bid, ask,
volume, open interest, and how old that quote was) for every subscribed instrument,
plus the spot and future. Greeks are NOT stored: they can be recomputed from these
prices. This is a sampled record, not every tick, so a backtest on it cannot see
what happened between samples.

Size: roughly 25 quotes per snapshot, compressed, a few MB per trading day at 5 s.
"""
from __future__ import annotations

import json
import logging
import threading
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import Float, Integer, LargeBinary, String, create_engine, delete, event, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .feed import Instr, Tick
from .greeks import IST

log = logging.getLogger(__name__)

# Bump if the stored layout changes, so old files are not silently misread.
SCHEMA_VERSION = 2  # v2: index/future tokens per snapshot + a contract archive (so old data can be replayed)


class _Base(DeclarativeBase):
    pass


class SnapshotRow(_Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)  # epoch seconds
    day: Mapped[str] = mapped_column(String(10), index=True)  # IST date
    underlying: Mapped[str] = mapped_column(String(20))
    expiry: Mapped[str] = mapped_column(String(10))
    spot: Mapped[float | None] = mapped_column(Float, nullable=True)
    future: Mapped[float | None] = mapped_column(Float, nullable=True)
    index_token: Mapped[str] = mapped_column(String(20), default="")
    future_token: Mapped[str] = mapped_column(String(20), default="")
    n_quotes: Mapped[int] = mapped_column(Integer)
    payload: Mapped[bytes] = mapped_column(LargeBinary)  # zlib(JSON)


class ContractRow(_Base):
    """What each stored token IS (strike, expiry, lot size...). The instrument master only lists live
    contracts, so without this archive an expired contract could never be replayed."""
    __tablename__ = "contracts"
    exchange: Mapped[str] = mapped_column(String(8), primary_key=True)
    token: Mapped[str] = mapped_column(String(20), primary_key=True)
    kind: Mapped[str] = mapped_column(String(4))  # OPT | FUT | IDX
    name: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(60))
    expiry: Mapped[str | None] = mapped_column(String(10), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)


@dataclass(frozen=True)
class Quote:
    ltp: float
    bid: float | None
    ask: float | None
    volume: int | None
    oi: int | None
    age: float  # seconds since this quote was last updated, at snapshot time


@dataclass(frozen=True)
class Snapshot:
    ts: float
    underlying: str
    expiry: str
    spot: float | None
    future: float | None
    quotes: dict[str, Quote]  # "NFO:40037" -> Quote
    index_token: str = ""
    future_token: str = ""


def _encode(quotes: dict[str, list]) -> bytes:
    return zlib.compress(json.dumps(quotes, separators=(",", ":")).encode())


def _decode(blob: bytes) -> dict[str, Quote]:
    return {k: Quote(*v) for k, v in json.loads(zlib.decompress(blob)).items()}


def open_sqlite(path: str | Path, user_version: int, writer_sync_full: bool):
    """Create/open a SQLite file with WAL and a schema-version guard. Shared by journal and snapshots."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=" + ("FULL" if writer_sync_full else "NORMAL"))
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    return engine


def check_schema_version(engine, expected: int, what: str) -> None:
    with engine.begin() as conn:
        found = conn.exec_driver_sql("PRAGMA user_version").scalar()
        if found == 0:
            conn.exec_driver_sql(f"PRAGMA user_version = {int(expected)}")
        elif found != expected:
            raise RuntimeError(f"{what} was created by a different version of this program "
                               f"(schema {found}, this program expects {expected}). Do not use it; back it up and ask for help.")


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._engine = open_sqlite(path, SCHEMA_VERSION, writer_sync_full=False)  # losing a few seconds of samples is fine
        _Base.metadata.create_all(self._engine)
        check_schema_version(self._engine, SCHEMA_VERSION, "Market snapshot database")
        self._lock = threading.Lock()

    def write(self, ts: float, underlying: str, expiry: str, spot: float | None, future: float | None,
              quotes: dict[str, list], index_token: str = "", future_token: str = "") -> None:
        row = SnapshotRow(ts=ts, day=datetime.fromtimestamp(ts, IST).date().isoformat(), underlying=underlying,
                          expiry=expiry, spot=spot, future=future, index_token=index_token, future_token=future_token,
                          n_quotes=len(quotes), payload=_encode(quotes))
        with self._lock, Session(self._engine) as s:
            s.add(row)
            s.commit()

    def iter_snapshots(self, start_ts: float | None = None, end_ts: float | None = None) -> Iterator[Snapshot]:
        """Oldest first. Used by the backtest module."""
        q = select(SnapshotRow).order_by(SnapshotRow.ts)
        if start_ts is not None:
            q = q.where(SnapshotRow.ts >= start_ts)
        if end_ts is not None:
            q = q.where(SnapshotRow.ts < end_ts)
        with Session(self._engine) as s:
            for r in s.scalars(q.execution_options(yield_per=500)):
                yield Snapshot(r.ts, r.underlying, r.expiry, r.spot, r.future, _decode(r.payload),
                               r.index_token, r.future_token)

    def register_contracts(self, rows: list[dict]) -> int:
        """Remember what tokens are (insert-only). Returns how many were new."""
        if not rows:
            return 0
        with self._lock, Session(self._engine) as s:
            have = {(r.exchange, r.token) for r in s.scalars(select(ContractRow))}
            new = [ContractRow(**{k: r.get(k) for k in ("exchange", "token", "kind", "name", "symbol", "expiry", "strike",
                                                         "opt_type", "lot_size", "tick_size")})
                   for r in rows if (r["exchange"], r["token"]) not in have]
            s.add_all(new)
            s.commit()
        return len(new)

    def load_contracts(self) -> list[dict]:
        with Session(self._engine) as s:
            return [{c.name: getattr(r, c.name) for c in ContractRow.__table__.columns} for r in s.scalars(select(ContractRow))]

    def stats(self) -> dict:
        with Session(self._engine) as s:
            n = s.scalar(select(func.count(SnapshotRow.id))) or 0
            days = list(s.scalars(select(SnapshotRow.day).distinct().order_by(SnapshotRow.day)))
        return {"snapshots": n, "days": days, "file_mb": round(self.path.stat().st_size / 1e6, 2) if self.path.exists() else 0}

    def prune(self, keep_days: int, today: date | None = None) -> int:
        """Delete snapshots older than keep_days. Returns rows deleted."""
        cutoff = ((today or datetime.now(IST).date()) - timedelta(days=keep_days)).isoformat()
        with self._lock, Session(self._engine) as s:
            n = s.execute(delete(SnapshotRow).where(SnapshotRow.day < cutoff)).rowcount
            s.commit()
        return n or 0


class SnapshotRecorder:
    """Feed callback: remembers the latest quote per instrument and saves a snapshot every N seconds."""

    def __init__(self, store: SnapshotStore, underlying: str, expiry: date, index: Instr, future: Instr,
                 interval_seconds: float = 5.0, describe: "Callable[[Instr], dict | None] | None" = None):
        self._store, self._underlying, self._expiry = store, underlying, expiry.isoformat()
        self._index, self._future, self._interval = index, future, interval_seconds
        self._describe = describe
        self._archived: set[Instr] = set()
        self._latest: dict[Instr, Tick] = {}
        self._last_written = 0.0
        self.written = 0

    def _archive_new_contracts(self) -> None:
        """Store what each token is, once, so this data stays replayable after the contracts expire."""
        if self._describe is None:
            return
        rows = []
        for ins in set(self._latest) - self._archived:
            d = self._describe(ins)
            if d is not None:
                rows.append(d)
        self._store.register_contracts(rows)
        self._archived |= set(self._latest)

    def on_tick(self, tick: Tick) -> None:
        self._latest[tick.instr] = tick
        if tick.received_at - self._last_written < self._interval:
            return
        # Need the underlying future to make a snapshot meaningful.
        if self._future not in self._latest:
            return
        now = tick.received_at
        quotes = {str(i): [t.ltp, t.bid, t.ask, t.volume, t.oi, round(max(0.0, now - t.received_at), 3)]
                  for i, t in self._latest.items() if i not in (self._index,)}
        spot = self._latest[self._index].ltp if self._index in self._latest else None
        try:
            self._archive_new_contracts()
            self._store.write(now, self._underlying, self._expiry, spot, self._latest[self._future].ltp, quotes,
                              self._index.token, self._future.token)
        except Exception:
            log.exception("Could not save a market snapshot")  # never let storage trouble reach the feed
            return
        self._last_written = now
        self.written += 1
