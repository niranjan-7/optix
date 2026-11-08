"""Trade journal (SQLite): every signal, decision, fill, and completed trade.

It is fed by two listeners and nothing else:
    risk.add_listener(journal.on_decision)    # every signal + approve/reject + reasons
    mgr.add_listener(journal.on_event)        # orders, fills, completed executions, closed positions
so it records what actually happened rather than what a strategy believed.

Tables
  signals     every signal the risk engine saw, with reasons. Refusals ARE the "rule violations".
  fills       every fill (price, quantity, fee)
  executions  how each approved signal ended up (complete? slippage per leg)
  trades      one row per closed position: entry/exit, P&L, fees, slippage - PLUS the
              evening-review fields `followed_rules` (yes/no) and `review_notes`
  events      everything else worth auditing (warnings, cancels, skipped legs)

Journal failures must never disturb trading: the callers already shield the engine, and
every handler here also catches and logs its own errors.

Back up `data/journal.db` (copy the file while the app is stopped, or use the .db plus its
-wal and -shm files together). Market snapshots live in a different file on purpose.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Float, Integer, String, Text, create_engine, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .analytics import Summary, TradeStat, compute_summary, format_summary
from .greeks import IST
from .risk import Decision
from .signals import Signal
from .snapshots import SnapshotStore, check_schema_version, open_sqlite

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1


# ================================================================== config

class SnapshotCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    interval_seconds: float = Field(5.0, gt=0)
    keep_days: int = Field(60, ge=1)


class JournalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    journal_file: str = "data/journal.db"
    market_file: str = "data/market.db"
    snapshots: SnapshotCfg = SnapshotCfg()


def load_journal_config(path: str | Path = "config/journal.yaml") -> JournalConfig:
    with open(path, encoding="utf-8") as f:
        return JournalConfig(**(yaml.safe_load(f) or {}))


# =================================================================== tables

class Base(DeclarativeBase):
    pass


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)  # ISO, IST
    idempotency_key: Mapped[str] = mapped_column(String(200), index=True)
    strategy: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(Text)
    legs_json: Mapped[str] = mapped_column(Text)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, index=True)
    codes: Mapped[str] = mapped_column(String(400), default="")  # comma-separated rejection codes
    detail: Mapped[str] = mapped_column(Text, default="")
    structure: Mapped[str | None] = mapped_column(String(30), nullable=True)
    loss_at_stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_loss: Mapped[float | None] = mapped_column(Float, nullable=True)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)
    exec_id: Mapped[str] = mapped_column(String(20))
    order_id: Mapped[str] = mapped_column(String(40))
    signal_key: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # ENTRY | EXIT
    strategy: Mapped[str] = mapped_column(String(80))
    position_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(60))
    side: Mapped[str] = mapped_column(String(4))
    lots: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    trade_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)


class ExecutionRow(Base):
    __tablename__ = "executions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)
    exec_id: Mapped[str] = mapped_column(String(20))
    signal_key: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    strategy: Mapped[str] = mapped_column(String(80))
    position_id: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    complete: Mapped[bool] = mapped_column(Boolean)
    legs_json: Mapped[str] = mapped_column(Text)
    slippage_cost: Mapped[float] = mapped_column(Float, default=0.0)  # rupees; + = paid more than the quote


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[str] = mapped_column(String(40), index=True)
    strategy: Mapped[str] = mapped_column(String(80))
    setup: Mapped[str] = mapped_column(String(160))
    structure: Mapped[str | None] = mapped_column(String(30), nullable=True)
    opened_at: Mapped[str] = mapped_column(String(40))
    closed_at: Mapped[str] = mapped_column(String(40), index=True)
    hold_minutes: Mapped[float] = mapped_column(Float)
    lots: Mapped[int] = mapped_column(Integer)
    entry_signal_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exit_signal_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    exit_reason: Mapped[str] = mapped_column(Text, default="")
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # net per unit
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_pnl: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    slippage_cost: Mapped[float] = mapped_column(Float, default=0.0)
    data_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- evening review, filled in by a person ---
    followed_rules: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[str] = mapped_column(Text)


# ================================================================== journal

def _leg_dict(l) -> dict[str, Any]:
    return {"symbol": l.symbol, "side": l.side, "lots": l.lots, "lot_size": l.lot_size, "strike": l.strike,
            "opt_type": l.opt_type, "expiry": l.expiry.isoformat(), "underlying": l.underlying}


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._engine = open_sqlite(path, SCHEMA_VERSION, writer_sync_full=True)  # trades must survive a power cut
        Base.metadata.create_all(self._engine)
        check_schema_version(self._engine, SCHEMA_VERSION, "Trade journal")
        self._lock = threading.RLock()

    def _session(self) -> Session:
        return Session(self._engine, expire_on_commit=False)

    # ---------------------------------------------------------- listeners
    def on_decision(self, signal: Signal, decision: Decision) -> None:
        """Risk-engine listener: record every signal with its outcome and reasons."""
        try:
            a = decision.order.assessment if decision.approved and decision.order else None
            row = SignalRow(
                ts=signal.created_at.astimezone(IST).isoformat(), idempotency_key=signal.idempotency_key,
                strategy=signal.strategy, source=signal.source, kind=signal.kind, reason=signal.reason,
                legs_json=json.dumps([_leg_dict(l) for l in signal.legs]), stop_loss=signal.stop_loss,
                target=signal.target, ref_price=signal.ref_price, position_id=signal.position_id,
                approved=decision.approved, codes=",".join(decision.codes), detail=decision.summary(),
                structure=a.structure if a else None, loss_at_stop=a.loss_at_stop if a else None,
                max_loss=a.max_loss if a else None)
            with self._lock, self._session() as s:
                s.add(row)
                s.commit()
        except Exception:
            log.exception("Journal could not record a decision")

    def on_event(self, ev: dict[str, Any]) -> None:
        """Execution listener."""
        try:
            kind = ev.get("type")
            with self._lock:
                if kind == "fill":
                    self._fill(ev)
                elif kind == "execution_done":
                    self._execution_done(ev)
                elif kind == "position_closed":
                    self._position_closed(ev)
                else:
                    self._generic(ev)
        except Exception:
            log.exception("Journal could not record an execution event")

    def _generic(self, ev: dict[str, Any]) -> None:
        with self._session() as s:
            s.add(EventRow(ts=ev.get("time", ""), type=ev["type"], payload=json.dumps(ev, default=str)))
            s.commit()

    def _fill(self, ev: dict[str, Any]) -> None:
        with self._session() as s:
            s.add(FillRow(ts=ev["time"], exec_id=ev["exec_id"], order_id=ev["order_id"], signal_key=ev["signal_key"],
                          kind=ev["kind"], strategy=ev["strategy"], position_id=ev.get("position_id"),
                          symbol=ev["symbol"], side=ev["side"], lots=ev["lots"], qty=ev["qty"],
                          price=ev["price"], fee=ev["fee"]))
            s.commit()

    def _execution_done(self, ev: dict[str, Any]) -> None:
        slip = sum((l.get("slippage_per_unit") or 0.0) * (l.get("filled_qty") or 0) for l in ev["legs"])
        with self._session() as s:
            s.add(ExecutionRow(ts=ev["time"], exec_id=ev["exec_id"], signal_key=ev["signal_key"], kind=ev["kind"],
                               strategy=ev["strategy"], position_id=ev.get("position_id"), complete=ev["complete"],
                               legs_json=json.dumps(ev["legs"]), slippage_cost=slip))
            s.commit()
        if ev.get("position_id"):
            self._refresh_slippage(ev["position_id"])  # an exit's slippage arrives after the position closes

    def _refresh_slippage(self, position_id: str) -> None:
        with self._session() as s:
            t = s.scalars(select(TradeRow).where(TradeRow.position_id == position_id).order_by(TradeRow.id.desc())).first()
            if t is None:
                return
            total = s.scalar(select(func.coalesce(func.sum(ExecutionRow.slippage_cost), 0.0)).where(
                ExecutionRow.position_id == position_id, ExecutionRow.ts >= t.opened_at))
            t.slippage_cost = float(total or 0.0)
            s.commit()

    def _signal_for(self, s: Session, key: str | None) -> SignalRow | None:
        if not key:
            return None
        return s.scalars(select(SignalRow).where(SignalRow.idempotency_key == key, SignalRow.approved.is_(True))
                         .order_by(SignalRow.id.desc())).first()

    def _position_closed(self, ev: dict[str, Any]) -> None:
        pid = ev["position_id"]
        with self._session() as s:
            fills = list(s.scalars(select(FillRow).where(FillRow.position_id == pid, FillRow.trade_id.is_(None))
                                   .order_by(FillRow.id)))
            entries = [f for f in fills if f.kind == "ENTRY"]
            exits = [f for f in fills if f.kind == "EXIT"]
            realised = float(ev["realised_pnl"])
            fees_total = float(ev["fees"])
            complete = bool(entries and exits)
            if fills:
                cost = sum(f.price * f.qty * (1 if f.side == "BUY" else -1) for f in entries)
                proceeds = sum(f.price * f.qty * (1 if f.side == "SELL" else -1) for f in exits)
                fees_total = sum(f.fee for f in fills)
                gross = proceeds - cost
                net = gross - fees_total
                if abs(net - realised) > 0.01:
                    complete = False
                    log.warning("Journal P&L for %s (%.2f) differs from the portfolio's (%.2f); using the portfolio's",
                                pid, net, realised)
                    net, gross = realised, realised + fees_total
                by_sym: Counter = Counter()
                for f in (entries or exits):
                    by_sym[f.symbol] += f.qty
                units = max(by_sym.values()) if by_sym else 0
                sample = (entries or exits)[0]
                lot_size = sample.qty // sample.lots if sample.lots else 1
                entry_price = cost / units if units and entries else None
                exit_price = proceeds / units if units and exits else None
                lots = units // lot_size if lot_size else 0
                opened_at = fills[0].ts
            else:
                gross, net, entry_price, exit_price, lots = realised + fees_total, realised, None, None, 0
                opened_at = ev["time"]
            esig = self._signal_for(s, entries[0].signal_key if entries else None)
            xsig = self._signal_for(s, exits[-1].signal_key if exits else None)
            strategy = ev.get("strategy") or (entries[0].strategy if entries else "unknown")
            opts = sorted({l["opt_type"] for l in json.loads(esig.legs_json)}) if esig else []
            structure = esig.structure if esig else None
            setup = " | ".join(x for x in (strategy, structure, "/".join(opts)) if x)
            t = TradeRow(
                position_id=pid, strategy=strategy, setup=setup, structure=structure, opened_at=opened_at,
                closed_at=ev["time"],
                hold_minutes=max(0.0, (datetime.fromisoformat(ev["time"]) - datetime.fromisoformat(opened_at)).total_seconds() / 60),
                lots=lots, entry_signal_key=esig.idempotency_key if esig else None,
                exit_signal_key=xsig.idempotency_key if xsig else None,
                entry_reason=esig.reason if esig else "", exit_reason=xsig.reason if xsig else "",
                entry_price=entry_price, exit_price=exit_price, gross_pnl=gross, fees=fees_total, net_pnl=net,
                data_complete=complete)
            s.add(t)
            s.flush()
            for f in fills:
                f.trade_id = t.id
            s.commit()
        self._refresh_slippage(pid)

    # ------------------------------------------------------------ queries
    def trades(self, start: date | None = None, end: date | None = None, unreviewed_only: bool = False) -> list[TradeRow]:
        q = select(TradeRow).order_by(TradeRow.closed_at, TradeRow.id)
        if start:
            q = q.where(TradeRow.closed_at >= start.isoformat())
        if end:
            q = q.where(TradeRow.closed_at < (end + timedelta(days=1)).isoformat())
        if unreviewed_only:
            q = q.where(TradeRow.followed_rules.is_(None))
        with self._session() as s:
            return list(s.scalars(q))

    def recent_signals(self, limit: int = 10) -> list[dict[str, Any]]:
        """Newest first. For the dashboard's 'what did the risk engine just decide' list."""
        with self._session() as s:
            rows = s.scalars(select(SignalRow).order_by(SignalRow.id.desc()).limit(limit))
            return [{"ts": r.ts, "strategy": r.strategy, "kind": r.kind, "source": r.source, "approved": r.approved,
                     "codes": [c for c in r.codes.split(",") if c], "detail": r.detail, "reason": r.reason}
                    for r in rows]

    def signal_counts(self, start: date, end: date) -> tuple[int, int, dict[str, int]]:
        lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
        with self._session() as s:
            rows = s.execute(select(SignalRow.approved, SignalRow.codes).where(SignalRow.ts >= lo, SignalRow.ts < hi)).all()
        codes: Counter = Counter()
        for approved, c in rows:
            if not approved:
                codes.update(x for x in c.split(",") if x)
        return len(rows), sum(1 for a, _ in rows if a), dict(codes)

    def summary(self, start: date, end: date) -> Summary:
        stats = [TradeStat(t.id, t.closed_at, t.setup, t.strategy, t.gross_pnl, t.fees, t.slippage_cost, t.net_pnl,
                           t.followed_rules) for t in self.trades(start, end)]
        total, approved, codes = self.signal_counts(start, end)
        return compute_summary(stats, start, end, total, approved, codes)

    def review(self, trade_id: int, followed_rules: bool | None, notes: str, reviewer: str,
               now: datetime | None = None) -> None:
        """Evening review: did this trade follow the rules? Anything worth remembering?"""
        with self._lock, self._session() as s:
            t = s.get(TradeRow, trade_id)
            if t is None:
                raise KeyError(f"no trade with id {trade_id}")
            if followed_rules is not None:
                t.followed_rules = followed_rules
            if notes:
                t.review_notes = (t.review_notes + "\n" if t.review_notes else "") + notes  # notes accumulate; never overwritten
            t.reviewed_at = (now or datetime.now(IST)).isoformat()
            t.reviewed_by = reviewer
            s.commit()

    def export_csv(self, path: str | Path, start: date | None = None, end: date | None = None) -> int:
        trades = self.trades(start, end)
        cols = [c.name for c in TradeRow.__table__.columns]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for t in trades:
                w.writerow([getattr(t, c) for c in cols])
        return len(trades)


# ====================================================================== CLI

def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _trade_line(t: TradeRow) -> str:
    rev = {None: "  ?  ", True: " yes ", False: " NO  "}[t.followed_rules]
    return (f"#{t.id:<4} {t.closed_at[:16].replace('T', ' ')}  {t.setup[:38]:<38} net {t.net_pnl:>+9,.0f}  "
            f"rules:{rev}")


def review_interactively(j: Journal, reviewer: str, input_fn: Callable[[str], str] = input,
                         out: Callable[[str], None] = print, now: datetime | None = None) -> int:
    todo = j.trades(unreviewed_only=True)
    if not todo:
        out("Nothing to review. All closed trades have been reviewed.")
        return 0
    out(f"{len(todo)} trade(s) to review. For each: did you (and the system) follow the rules?\n")
    done = 0
    for t in todo:
        out(_trade_line(t))
        out(f"      why in : {t.entry_reason or '(not recorded)'}")
        out(f"      why out: {t.exit_reason or '(not recorded)'}")
        out(f"      entry {t.entry_price if t.entry_price is not None else 'n/a'} -> exit "
            f"{t.exit_price if t.exit_price is not None else 'n/a'}, held {t.hold_minutes:.0f} min, "
            f"fees {t.fees:,.0f}, slippage {t.slippage_cost:,.0f}")
        while True:
            ans = input_fn("      Followed the rules?  [y] yes  [n] no  [s] skip  [q] quit > ").strip().lower()
            if ans in ("y", "n", "s", "q"):
                break
            out("      Please type y, n, s or q.")
        if ans == "q":
            break
        if ans == "s":
            continue
        notes = input_fn("      Notes (optional, Enter to skip) > ").strip()
        j.review(t.id, ans == "y", notes, reviewer, now)
        done += 1
        out("")
    out(f"Saved {done} review(s).")
    return done


def _cli(argv: list[str] | None = None, input_fn: Callable[[str], str] = input, today: date | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trader.journal", description="Trade journal and weekly summary")
    ap.add_argument("--config", default="config/journal.yaml")
    ap.add_argument("--journal", help="journal database file (default from config)")
    ap.add_argument("--market", help="market snapshot database file (default from config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def period(p: argparse.ArgumentParser) -> None:
        p.add_argument("--days", type=int, default=7, help="last N days including today (default 7)")
        p.add_argument("--from", dest="start", type=_parse_day, help="first day, YYYY-MM-DD")
        p.add_argument("--to", dest="end", type=_parse_day, help="last day, YYYY-MM-DD")

    period(sub.add_parser("summary", help="win rate, average win/loss, drawdown, P&L by setup"))
    t = sub.add_parser("trades", help="list closed trades"); period(t)
    t.add_argument("--unreviewed", action="store_true")
    r = sub.add_parser("review", help="evening review: did each trade follow the rules?")
    r.add_argument("--by", default="operator", help="your name, stored with the review")
    r.add_argument("--id", type=int, help="review one trade non-interactively")
    r.add_argument("--followed", choices=["y", "n"], help="with --id")
    r.add_argument("--notes", default="", help="with --id")
    e = sub.add_parser("export", help="write trades to a CSV file for Excel"); period(e)
    e.add_argument("--csv", required=True)
    sub.add_parser("snapshots", help="show stored market snapshots")
    pr = sub.add_parser("prune-snapshots", help="delete old market snapshots")
    pr.add_argument("--keep-days", type=int)
    args = ap.parse_args(argv)

    cfg = load_journal_config(args.config) if Path(args.config).exists() else JournalConfig()
    today = today or datetime.now(IST).date()

    def bounds() -> tuple[date, date]:
        end = args.end or today
        start = args.start or (end - timedelta(days=args.days - 1))
        return start, end

    if args.cmd in ("snapshots", "prune-snapshots"):
        store = SnapshotStore(args.market or cfg.market_file)
        if args.cmd == "prune-snapshots":
            n = store.prune(args.keep_days or cfg.snapshots.keep_days, today)
            print(f"Deleted {n} old snapshot(s).")
        st = store.stats()
        print(f"{st['snapshots']} snapshots over {len(st['days'])} day(s); file {st['file_mb']} MB")
        if st["days"]:
            print(f"first {st['days'][0]}  last {st['days'][-1]}")
        return 0

    j = Journal(args.journal or cfg.journal_file)
    if args.cmd == "summary":
        s, e_ = bounds()
        print(format_summary(j.summary(s, e_)))
    elif args.cmd == "trades":
        s, e_ = bounds()
        rows = j.trades(s, e_, args.unreviewed)
        print("\n".join(_trade_line(x) for x in rows) if rows else "No trades in that period.")
    elif args.cmd == "review":
        if args.id is None:
            review_interactively(j, args.by, input_fn)
        else:
            if args.followed is None and not args.notes:
                print("Give --followed y|n and/or --notes", file=sys.stderr)
                return 2
            try:
                j.review(args.id, None if args.followed is None else args.followed == "y", args.notes, args.by)
            except KeyError as ex:
                print(ex, file=sys.stderr)
                return 1
            print(f"Trade #{args.id} reviewed.")
    elif args.cmd == "export":
        s, e_ = bounds()
        n = j.export_csv(args.csv, s, e_)
        print(f"Wrote {n} trade(s) to {args.csv}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(_cli())
