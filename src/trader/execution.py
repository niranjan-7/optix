"""Execution manager: turns risk-approved orders into broker orders and tracks them.

The flow for every signal (rule 2):
    strategy / webhook -> ExecutionManager.submit() -> RiskEngine.evaluate() -> broker

Safety properties that live here (the "why"):
* BUY legs are placed before SELL legs, always. Opening a spread: buy the
  protective wing, then sell the short. Closing: buy back the short, then sell
  the long. So protection never disappears before the risk it covers.
* A SELL leg is only placed to the extent that doing so does not leave more
  short lots uncovered than before (`coverage_deficit`). If a protective buy
  only partly filled, we sell correspondingly less - or nothing.
* Fills update the portfolio as they happen, so a kill switch pressed mid-order
  sees (and closes) what has really been bought.
* Every order is a marketable LIMIT with a buffer, never a blind market order.
* Square-off exits go through the risk engine like everything else; the engine
  simply never blocks a genuine exit.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .brokers import (
    CANCELLED, FILLED, REJECTED, TICK, BrokerAdapter, BrokerOrder, PaperConfig, round_down, round_up,
)
from .feed import Tick
from .greeks import IST
from .portfolio import FeeModel, Portfolio
from .risk import ApprovedOrder, Decision, RiskEngine, exit_key
from .signals import Leg, PositionView, Signal
from .structures import coverage_deficit

log = logging.getLogger(__name__)


# ================================================================== config

class OrdersCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit_buffer_pct: float = Field(1.0, ge=0, le=20)
    emergency_buffer_pct: float = Field(5.0, ge=0, le=50)
    order_timeout_seconds: float = Field(20, gt=0)
    flatten_retry_seconds: float = Field(10, gt=0)


class LiveCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_type: str = "INTRADAY"


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    orders: OrdersCfg = OrdersCfg()
    paper: PaperConfig = PaperConfig()
    fees: FeeModel = FeeModel()
    live: LiveCfg = LiveCfg()
    portfolio_file: str = "data/portfolio.json"


def load_execution_config(path: str | Path = "config/execution.yaml") -> ExecutionConfig:
    with open(path, encoding="utf-8") as f:
        return ExecutionConfig(**(yaml.safe_load(f) or {}))


# ============================================================== bookkeeping

@dataclass
class _LegRun:
    leg: Leg
    planned_lots: int = 0
    order: BrokerOrder | None = None
    ref_price: float | None = None  # touch price when we placed the order (for slippage)
    applied_qty: int = 0  # units already booked in the portfolio
    applied_value: float = 0.0
    first_fill_booked: bool = False
    cancel_sent: bool = False
    note: str = ""  # why nothing was placed / what went wrong

    @property
    def terminal(self) -> bool:
        return self.order is None or self.order.is_terminal


@dataclass
class _Exec:
    exec_id: str
    approved: ApprovedOrder
    emergency: bool
    stage: int = 1  # 1 = BUY legs, 2 = SELL legs
    runs: list[_LegRun] = field(default_factory=list)
    position_id: str | None = None
    stage_started: float = 0.0
    cancel_requested: bool = False

    @property
    def signal(self) -> Signal:
        return self.approved.signal


Event = dict[str, Any]


class ExecutionManager:
    def __init__(self, risk: RiskEngine, broker: BrokerAdapter, portfolio: Portfolio,
                 quotes: Callable[[str], Tick | None], config: ExecutionConfig | None = None,
                 clock: Callable[[], float] = time.time):
        self._risk, self._broker, self._portfolio, self._quotes = risk, broker, portfolio, quotes
        self.cfg = config or ExecutionConfig()
        self._clock = clock
        self._lock = threading.RLock()
        self._active: list[_Exec] = []
        self._listeners: list[Callable[[Event], None]] = []
        self._n = 0
        self._last_flatten: dict[str, float] = {}

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        """Receives plain-dict events (order_placed, fill, execution_done, warning...) for the journal/alerts."""
        self._listeners.append(fn)

    def _emit(self, type_: str, **data: Any) -> None:
        # Stamped with the injected clock (not the wall clock) so replays and tests have coherent times.
        ev: Event = {"type": type_, "time": datetime.fromtimestamp(self._clock(), IST).isoformat(), **data}
        log.info("EXEC %s %s", type_, {k: v for k, v in data.items() if k != "legs"})
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                log.exception("Execution listener failed")

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    # ----------------------------------------------------------- entry point
    def submit(self, signal: Signal) -> Decision:
        """The ONLY way a signal becomes orders. Pass this to StrategyRunner / the webhook."""
        with self._lock:
            decision = self._risk.evaluate(signal)
            if decision.approved:
                assert decision.order is not None
                self._begin(decision.order)
            return decision

    # ------------------------------------------------------------- starting
    def _begin(self, approved: ApprovedOrder) -> None:
        sig = approved.signal
        self._n += 1
        ex = _Exec(f"X{self._n}", approved, emergency=(sig.source == "system"), stage_started=self._clock())
        # BUY legs first, always (see module docstring).
        ordered = [l for l in sig.legs if l.side == "BUY"] + [l for l in sig.legs if l.side == "SELL"]
        ex.runs = [_LegRun(l) for l in ordered]
        if sig.kind == "EXIT":
            ex.position_id = sig.position_id or self._resolve_position(sig)
        if not any(l.side == "BUY" for l in ordered):
            ex.stage = 2  # e.g. closing a plain long: nothing to buy first
        self._active.append(ex)
        self._emit("execution_started", exec_id=ex.exec_id, signal_key=sig.idempotency_key, kind=sig.kind,
                   strategy=sig.strategy, position_id=ex.position_id)
        self._place_stage(ex)
        if ex.stage == 2 and all(r.terminal for r in ex.runs):
            self._finish(ex)  # nothing could be placed at all

    def _resolve_position(self, sig: Signal) -> str | None:
        for pos in self._portfolio.open_positions():
            held = {(l.token, l.side) for l in pos.legs}
            if all((l.token, "SELL" if l.side == "BUY" else "BUY") in held for l in sig.legs):
                return pos.position_id
        return None

    def _holdings(self, ex: _Exec) -> dict[tuple, int]:
        """Signed lots per contract in the scope this execution can affect."""
        held: dict[tuple, int] = {}
        for pos in self._portfolio.open_positions():
            if ex.position_id is not None and pos.position_id != ex.position_id:
                continue
            if ex.position_id is None and ex.signal.kind == "ENTRY":
                continue  # nothing of this new position exists yet
            for l in pos.legs:
                k = (l.underlying, l.expiry, l.strike, l.opt_type)
                held[k] = held.get(k, 0) + (l.lots if l.side == "BUY" else -l.lots)
        return held

    def _limit_price(self, leg: Leg, emergency: bool) -> tuple[float | None, float | None]:
        q = self._quotes(leg.token)
        if q is None:
            return None, None
        buf = (self.cfg.orders.emergency_buffer_pct if emergency else self.cfg.orders.limit_buffer_pct) / 100
        if leg.side == "BUY":
            touch = q.ask or q.ltp
            return round_up(touch * (1 + buf)), touch
        touch = q.bid or q.ltp
        return max(TICK, round_down(touch * (1 - buf))), touch

    @staticmethod
    def _no_worse(held: dict[tuple, int], key: tuple, new_value: int, before: dict[tuple, int]) -> bool:
        trial = dict(held)
        trial[key] = new_value
        return all(n <= before.get(g, 0) for g, n in coverage_deficit(trial).items())

    def _place_stage(self, ex: _Exec) -> None:
        side = "BUY" if ex.stage == 1 else "SELL"
        held = self._holdings(ex)
        before = coverage_deficit(held)
        for run in (r for r in ex.runs if r.leg.side == side):
            key = (run.leg.underlying, run.leg.expiry, run.leg.strike, run.leg.opt_type)
            lots = run.leg.lots
            have = held.get(key, 0)
            if side == "SELL":
                if ex.signal.kind == "EXIT":
                    lots = min(lots, max(have, 0))  # cannot sell more than the long we hold
                # Whether opening a short or closing a long: never leave MORE short lots
                # uncovered than before. Sell fewer lots (or none) until that holds.
                while lots > 0 and not self._no_worse(held, key, have - lots, before):
                    lots -= 1
            elif ex.signal.kind == "EXIT":
                lots = min(lots, max(-have, 0))  # cannot buy back more than the short we hold
            run.planned_lots = lots
            if lots < 1:
                run.note = "not placed: nothing to close" if ex.signal.kind == "EXIT" else "not placed: would leave a short option uncovered"
                self._emit("leg_skipped", exec_id=ex.exec_id, symbol=run.leg.symbol, side=run.leg.side, reason=run.note)
                continue
            held[key] = have + lots if side == "BUY" else have - lots
            limit, touch = self._limit_price(run.leg, ex.emergency)
            if limit is None:
                run.note = "not placed: no market quote to price the order"
                self._emit("warning", exec_id=ex.exec_id, message=f"{run.leg.symbol}: {run.note}")
                continue
            try:
                run.order = self._broker.place_order(ex.approved, run.leg, lots, limit)
            except Exception as e:
                run.note = f"broker error: {type(e).__name__}: {e}"
                log.exception("place_order failed for %s", run.leg.symbol)
                self._emit("warning", exec_id=ex.exec_id, message=f"{run.leg.symbol}: {run.note}")
                continue
            run.ref_price = touch
            self._emit("order_placed", exec_id=ex.exec_id, order_id=run.order.order_id, symbol=run.leg.symbol,
                       side=run.leg.side, lots=lots, limit_price=limit, status=run.order.status)
        ex.stage_started = self._clock()

    # --------------------------------------------------------------- polling
    def poll(self) -> None:
        """Call about once a second from the scheduler."""
        with self._lock:
            for ex in list(self._active):
                try:
                    self._advance(ex)
                except Exception:
                    log.exception("Error advancing execution %s", ex.exec_id)
                    self._emit("warning", exec_id=ex.exec_id, message="internal error while tracking orders; CHECK POSITIONS")
            self._flatten_if_needed()

    def _advance(self, ex: _Exec) -> None:
        side = "BUY" if ex.stage == 1 else "SELL"
        runs = [r for r in ex.runs if r.leg.side == side]
        timed_out = self._clock() - ex.stage_started > self.cfg.orders.order_timeout_seconds
        for r in runs:
            if r.order is None:
                continue
            if not r.order.is_terminal:
                try:
                    r.order = self._broker.get_order(r.order.order_id)
                except Exception as e:
                    log.warning("Could not read order %s: %s", r.order.order_id, e)
                    continue
            self._book_fills(ex, r)
            if not r.order.is_terminal and not r.cancel_sent and (timed_out or ex.cancel_requested):
                try:
                    self._broker.cancel_order(r.order.order_id)
                    r.cancel_sent = True
                    self._emit("order_cancel_sent", exec_id=ex.exec_id, order_id=r.order.order_id,
                               reason="kill/cancel" if ex.cancel_requested else "timeout")
                except Exception as e:
                    log.warning("Cancel of %s failed: %s", r.order.order_id, e)
        if not all(r.terminal for r in runs):
            return
        if ex.stage == 1:
            ex.stage = 2
            if ex.cancel_requested:
                self._finish(ex)
                return
            self._place_stage(ex)
            if all(r.terminal for r in ex.runs if r.leg.side == "SELL"):
                self._finish(ex)  # nothing was placed (or all rejected) in stage 2
        else:
            self._finish(ex)

    def _book_fills(self, ex: _Exec, r: _LegRun) -> None:
        o = r.order
        assert o is not None
        new_qty = o.filled_qty - r.applied_qty  # units filled since we last looked
        lots = new_qty // o.lot_size
        if lots < 1:
            return
        qty = lots * o.lot_size
        # Price of just the new fills, from the order's cumulative average.
        price = (o.avg_price * o.filled_qty - r.applied_value) / new_qty
        first = not r.first_fill_booked
        try:
            if ex.signal.kind == "ENTRY":
                ex.position_id, fee = self._portfolio.record_entry_fill(
                    ex.position_id, ex.signal.strategy, ex.signal.stop_loss, ex.signal.target, r.leg, lots, price, first)
                pnl = None
            else:
                closed = None
                if ex.position_id is None:
                    raise ValueError("no position found to apply this exit fill to")
                pnl, fee, closed = self._portfolio.record_exit_fill(ex.position_id, r.leg, lots, price, first)
        except Exception as e:
            log.exception("Could not book fill for %s", o.order_id)
            self._emit("warning", exec_id=ex.exec_id,
                       message=f"FILL NOT BOOKED for {r.leg.symbol} ({lots} lots @ {price:.2f}): {e}. CHECK POSITIONS AT BROKER")
            r.applied_qty += qty  # don't retry forever; the warning is the signal to a human
            r.applied_value += price * qty
            return
        r.applied_qty += qty
        r.applied_value += price * qty
        r.first_fill_booked = True
        self._emit("fill", exec_id=ex.exec_id, order_id=o.order_id, symbol=r.leg.symbol, side=r.leg.side,
                   lots=lots, qty=qty, price=round(price, 2), fee=round(fee, 2), position_id=ex.position_id,
                   realised=pnl, signal_key=ex.signal.idempotency_key, kind=ex.signal.kind,
                   strategy=ex.signal.strategy)
        if ex.signal.kind == "EXIT" and closed is not None:
            # Emitted AFTER the fill that closed it, so anything listening (the journal) already holds every fill.
            self._emit("position_closed", position_id=closed.position_id, strategy=closed.strategy,
                       realised_pnl=closed.realised_pnl, fees=closed.fees)

    def _finish(self, ex: _Exec) -> None:
        sig = ex.signal
        legs = []
        complete = True
        for r in ex.runs:
            filled = r.applied_qty // r.leg.lot_size
            avg = r.applied_value / r.applied_qty if r.applied_qty else None
            slip = None
            if avg is not None and r.ref_price is not None:
                slip = round((avg - r.ref_price) if r.leg.side == "BUY" else (r.ref_price - avg), 4)  # + = cost
            if filled < r.leg.lots:
                complete = False
            status = r.order.status if r.order else "NOT_PLACED"
            legs.append({"symbol": r.leg.symbol, "side": r.leg.side, "wanted_lots": r.leg.lots, "filled_lots": filled,
                         "filled_qty": r.applied_qty,
                         "avg_price": None if avg is None else round(avg, 2), "ref_price": r.ref_price,
                         "slippage_per_unit": slip, "status": status, "note": r.note or (r.order.message if r.order else "")})
        self._active.remove(ex)
        try:
            if sig.kind == "ENTRY":
                self._risk.entry_settled(sig.idempotency_key)
            else:
                self._risk.exit_settled(exit_key(sig))
        except Exception:
            log.exception("Could not tell the risk engine that %s settled", sig.idempotency_key)
        self._emit("execution_done", exec_id=ex.exec_id, signal_key=sig.idempotency_key, kind=sig.kind,
                   strategy=sig.strategy, position_id=ex.position_id, complete=complete, legs=legs)
        if not complete:
            any_fill = any(l["filled_lots"] for l in legs)
            self._emit("warning", exec_id=ex.exec_id,
                       message=f"{sig.kind} {sig.idempotency_key} was {'only PARTLY' if any_fill else 'NOT'} filled."
                               + (" Review the position: it may no longer match the intended structure." if any_fill and sig.kind == 'ENTRY' else ""))

    # --------------------------------------------------- kill-switch actions
    def cancel_all_pending(self) -> None:
        """KillActions: cancel everything open and stop any execution from placing further stages."""
        with self._lock:
            n = self._broker.cancel_all()
            for ex in self._active:
                ex.cancel_requested = True
            self._emit("cancel_all", broker_orders_cancelled=n, executions_halted=len(self._active))

    def square_off_all(self) -> None:
        """KillActions: submit an EXIT for every open position, through the risk engine."""
        with self._lock:
            for pos in self._portfolio.open_positions():
                self._square_off(pos)

    def _exit_active(self, position_id: str) -> bool:
        return any(e.signal.kind == "EXIT" and e.position_id == position_id for e in self._active)

    def _square_off(self, pos: PositionView) -> None:
        if self._exit_active(pos.position_id):
            return
        now = datetime.now(IST)
        sig = Signal(
            idempotency_key=f"squareoff:{pos.position_id}:{int(self._clock() * 1000)}",
            strategy=pos.strategy, kind="EXIT", legs=tuple(l.opposite() for l in pos.legs),
            reason="system square-off (kill switch / daily stop / end of day)", created_at=now,
            position_id=pos.position_id, source="system")
        d = self.submit(sig)
        if not d.approved:
            self._emit("warning", message=f"square-off of {pos.position_id} was rejected: {d.summary()}. CLOSE IT MANUALLY")

    def _flatten_if_needed(self) -> None:
        """While positions must be closed, keep (re)trying for stragglers."""
        try:
            if not self._risk.must_be_flat():
                return
        except Exception:
            log.exception("could not read the risk state")
            return
        now = self._clock()
        for pos in self._portfolio.open_positions():
            if self._exit_active(pos.position_id):
                continue
            if now - self._last_flatten.get(pos.position_id, -1e9) < self.cfg.orders.flatten_retry_seconds:
                continue
            self._last_flatten[pos.position_id] = now
            self._square_off(pos)

    # ----------------------------------------------------------- start-up
    def recover(self) -> list[str]:
        """Run once at startup: cancel stale broker orders and compare positions with the broker.

        Returns human-readable mismatches (empty = consistent, or the broker can't say).
        """
        with self._lock:
            n = self._broker.cancel_all()
            if n:
                log.warning("Startup: cancelled %d leftover open order(s) at the broker", n)
            return self.reconcile()

    def reconcile(self) -> list[str]:
        theirs = self._broker.net_positions()
        if theirs is None:
            return []
        mine = self._portfolio.net_units_by_token()
        issues = []
        for tok in sorted(set(mine) | set(theirs)):
            if mine.get(tok, 0) != theirs.get(tok, 0):
                issues.append(f"token {tok}: we think {mine.get(tok, 0):+d} units, broker says {theirs.get(tok, 0):+d}")
        for i in issues:
            self._emit("warning", message=f"POSITION MISMATCH with broker: {i}")
        return issues
