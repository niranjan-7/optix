"""The risk engine: the single gate between ideas (signals) and orders.

Rule 2 of the project: every signal - from a strategy OR the webhook - passes
through `RiskEngine.evaluate()`, and nothing reaches a broker without the
`ApprovedOrder` it returns. Principles behind the design:

* FAIL CLOSED. Any unexpected error rejects the signal. An unreadable state
  file starts the system with the kill switch ON.
* ENTRIES are limited; EXITS are never blocked by limits, but an "exit" must
  genuinely reduce an existing position (otherwise a mislabelled exit could
  create a naked short).
* ALL reasons are reported, not just the first, so the operator sees the full
  picture; every rejection is logged with its reasons (and handed to listeners
  such as the journal and Telegram alerts).
* Safety state (kill switch, daily halt, order counts, idempotency keys) lives
  in a small JSON file, read fresh and updated under a file lock on every
  decision. So it survives restarts and a separate `kill` command-line process
  takes effect immediately.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sys
import threading
import time as time_mod
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .feed import HealthStatus, Tick
from .greeks import IST
from .signals import Leg, PositionView, Signal
from .structures import (
    ALL_KINDS, LONG_OPTION, StructureError, check_stop_and_target, classify, coverage_deficit, value_structure,
)

try:  # POSIX only; on Windows the engine still works, just without cross-process locking.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


# ================================================================= config

def _quoted_time(v: Any) -> Any:
    # YAML reads unquoted 10:30 as the integer 630; refuse rather than misread.
    if not isinstance(v, (str, time)):
        raise ValueError('write times as quoted strings, e.g. "09:20"')
    return v


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_loss_per_trade: float = Field(gt=0)
    max_structure_loss: float = Field(gt=0)
    max_loss_per_day: float = Field(gt=0)
    square_off_on_daily_loss: bool = True
    max_open_positions: int = Field(ge=1)
    max_lots_per_order: int = Field(ge=1)
    max_orders_per_day: int = Field(ge=1)

    @model_validator(mode="after")
    def _sane(self) -> "Limits":
        if self.max_loss_per_trade > self.max_loss_per_day:
            raise ValueError("max_loss_per_trade must not exceed max_loss_per_day")
        if self.max_loss_per_trade > self.max_structure_loss:
            raise ValueError("max_loss_per_trade must not exceed max_structure_loss")
        return self


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekdays: list[int] = [0, 1, 2, 3, 4]
    trading_start: time
    no_new_entries_after: time
    trading_end: time
    force_square_off: time

    @field_validator("weekdays", mode="before")
    @classmethod
    def _names(cls, v: Any) -> Any:
        out = []
        for d in v:
            if isinstance(d, str):
                if d not in _WEEKDAYS:
                    raise ValueError(f"unknown weekday {d!r}; use Mon..Sun")
                d = _WEEKDAYS[d]
            if not isinstance(d, int) or not 0 <= d <= 6:
                raise ValueError(f"bad weekday {d!r}; use Mon..Sun")
            out.append(d)
        return out

    @field_validator("trading_start", "no_new_entries_after", "trading_end", "force_square_off", mode="before")
    @classmethod
    def _q(cls, v: Any) -> Any:
        return _quoted_time(v)

    @model_validator(mode="after")
    def _order(self) -> "Window":
        if not (self.trading_start < self.no_new_entries_after <= self.trading_end <= self.force_square_off):
            raise ValueError("need trading_start < no_new_entries_after <= trading_end <= force_square_off")
        return self


class ExpiryDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_new_entries: bool = False
    max_lots_per_order: int = Field(1, ge=1)


class QuoteRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_spread_pct: float = Field(gt=0)
    max_spread_abs: float = Field(gt=0)
    max_quote_age_seconds: float = Field(gt=0)


class StructureRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: list[str]

    @field_validator("allowed")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        bad = [k for k in v if k not in ALL_KINDS]
        if bad:
            raise ValueError(f"unknown structure(s) {bad}; known: {list(ALL_KINDS)}")
        return v


class Inflight(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pending_ttl_seconds: float = Field(60, gt=0)


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    limits: Limits
    window: Window
    expiry_day: ExpiryDay = ExpiryDay()
    quotes: QuoteRules
    structures: StructureRules
    inflight: Inflight = Inflight()
    state_file: str = "data/risk_state.json"


def load_risk_config(path: str | Path = "config/risk.yaml") -> RiskConfig:
    with open(path, encoding="utf-8") as f:
        return RiskConfig(**(yaml.safe_load(f) or {}))


# ============================================================= decisions

def exit_key(signal: Signal) -> str:
    """Key under which an in-flight exit is tracked (shared with execution)."""
    return signal.position_id or "legs:" + "|".join(sorted(f"{l.token}{l.side}{l.lots}" for l in signal.legs))


class Code:
    KILL_SWITCH = "KILL_SWITCH"
    DAY_HALTED = "DAY_HALTED"
    FEED_UNHEALTHY = "FEED_UNHEALTHY"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    EXPIRY_DAY_BLOCKED = "EXPIRY_DAY_BLOCKED"
    DUPLICATE = "DUPLICATE"
    NOT_DEFINED_RISK = "NOT_DEFINED_RISK"
    STRUCTURE_DISABLED = "STRUCTURE_DISABLED"
    NO_QUOTE = "NO_QUOTE"
    STALE_QUOTE = "STALE_QUOTE"
    SPREAD_UNKNOWN = "SPREAD_UNKNOWN"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    BAD_PRICING = "BAD_PRICING"
    TOO_MANY_LOTS = "TOO_MANY_LOTS"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_ORDERS = "MAX_ORDERS"
    NO_STOP_LOSS = "NO_STOP_LOSS"
    BAD_STOP_OR_TARGET = "BAD_STOP_OR_TARGET"
    MAX_LOSS_PER_TRADE = "MAX_LOSS_PER_TRADE"
    MAX_STRUCTURE_LOSS = "MAX_STRUCTURE_LOSS"
    EXIT_NOT_COVERED = "EXIT_NOT_COVERED"
    UNKNOWN_POSITION = "UNKNOWN_POSITION"
    EXIT_IN_FLIGHT = "EXIT_IN_FLIGHT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class Rejection:
    code: str
    detail: str


@dataclass(frozen=True)
class RiskAssessment:
    structure: str | None
    net_debit_per_unit: float | None  # >0 debit, <0 credit
    loss_at_stop: float | None  # rupees, whole position
    max_loss: float | None  # rupees, whole position (worst case)


# The seal proves an ApprovedOrder was minted by this module in this process.
# It guards against ACCIDENTAL bypass (a code path building an order itself);
# it is not a defence against someone deliberately editing the source.
_SEAL_KEY = secrets.token_bytes(32)


def _seal(order_id: str, signal: Signal, approved_at: datetime) -> str:
    msg = f"{order_id}|{approved_at.isoformat()}|{signal!r}".encode()
    return hmac.new(_SEAL_KEY, msg, hashlib.sha256).hexdigest()


class UnapprovedOrderError(RuntimeError):
    """A broker adapter was handed something the risk engine did not approve."""


@dataclass(frozen=True)
class ApprovedOrder:
    order_id: str
    signal: Signal
    approved_at: datetime
    assessment: RiskAssessment
    seal: str

    def verify(self) -> bool:
        return hmac.compare_digest(self.seal, _seal(self.order_id, self.signal, self.approved_at))


def require_approved(order: object) -> ApprovedOrder:
    """Broker adapters call this first. Raises unless the engine really approved `order`."""
    if not isinstance(order, ApprovedOrder) or not order.verify():
        raise UnapprovedOrderError("order was not approved by the risk engine; refusing to send")
    return order


@dataclass(frozen=True)
class Decision:
    approved: bool
    reasons: tuple[Rejection, ...]
    order: ApprovedOrder | None

    def summary(self) -> str:
        if self.approved:
            return "approved"
        return "; ".join(f"[{r.code}] {r.detail}" for r in self.reasons)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)


# ============================================================ state store

@dataclass
class RiskState:
    kill_active: bool = False
    kill_reason: str = ""
    kill_source: str = ""
    kill_at: str = ""
    kill_actioned: bool = True  # False => actions (cancel + square off) still owed
    halted_day: str | None = None
    halt_reason: str = ""
    halt_actioned: bool = True
    eod_day: str | None = None  # day on which the forced end-of-day square-off was done
    day: str | None = None
    orders_today: int = 0
    keys: dict[str, str] = field(default_factory=dict)  # idempotency key -> approved-at (ISO)
    pending_entries: dict[str, float] = field(default_factory=dict)  # key -> epoch
    pending_exits: dict[str, float] = field(default_factory=dict)  # position id/legs -> epoch


class RiskStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lockpath = self.path.with_suffix(self.path.suffix + ".lock")

    def _load(self) -> RiskState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(RiskState)}
            return RiskState(**{k: v for k, v in raw.items() if k in known})
        except FileNotFoundError:
            return RiskState()
        except Exception as e:  # corrupt / unreadable: fail CLOSED
            backup = self.path.with_name(f"{self.path.name}.corrupt.{int(time_mod.time())}")
            with contextlib.suppress(OSError):
                os.replace(self.path, backup)
            log.critical("Risk state file unreadable (%s); kill switch forced ON. Kept a copy at %s", e, backup)
            return RiskState(kill_active=True, kill_source="system", kill_actioned=False,
                             kill_reason=f"risk state file was unreadable ({type(e).__name__}); inspect {backup.name}, then reset manually",
                             kill_at=datetime.now(IST).isoformat())

    def _save(self, st: RiskState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(st), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: readers never see half a file

    @contextlib.contextmanager
    def transaction(self) -> Iterator[RiskState]:
        """Load fresh state under a file lock; save only if the body changed it and succeeded."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lockpath, "a") as lockf:
            if fcntl:
                fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                st = self._load()
                before = asdict(st)
                yield st
                if asdict(st) != before:
                    self._save(st)
            finally:
                if fcntl:
                    fcntl.flock(lockf, fcntl.LOCK_UN)


# ================================================================ protocols

class RiskPortfolio(Protocol):
    def open_positions(self) -> list[PositionView]: ...
    def realised_pnl_today(self) -> float: ...
    def unrealised_pnl(self) -> float: ...


class KillActions(Protocol):
    """Implemented by the execution module.

    `square_off_all` MUST close positions by submitting EXIT signals through
    `RiskEngine.evaluate` like everything else (exits are never blocked).
    """
    def cancel_all_pending(self) -> None: ...
    def square_off_all(self) -> None: ...


# ================================================================== engine

class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        portfolio: RiskPortfolio,
        quotes: Callable[[str], Tick | None],  # NFO token -> latest tick
        feed_health: Callable[[], HealthStatus],
        store: RiskStateStore | None = None,
        actions: KillActions | None = None,
        notifier: Callable[[str, str], None] | None = None,  # (level, message)
        clock: Callable[[], datetime] | None = None,
    ):
        self.cfg = config
        self._portfolio, self._quotes, self._feed_health = portfolio, quotes, feed_health
        self._store = store or RiskStateStore(config.state_file)
        self._actions, self._notifier = actions, notifier
        self._clock = clock or (lambda: datetime.now(IST))
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Signal, Decision], None]] = []
        self._last_action_fail_notice = 0.0

    def set_notifier(self, notifier: Callable[[str, str], None]) -> None:
        """Where kill / daily-stop / failed-emergency messages go (e.g. Telegram). Set after construction."""
        self._notifier = notifier

    def set_actions(self, actions: KillActions) -> None:
        """The execution manager needs the engine and the engine needs it; wire after both exist."""
        self._actions = actions

    def must_be_flat(self) -> bool:
        """True when open positions are supposed to be closed (kill, daily halt, or end of day)."""
        with self._lock:
            now = self._clock().astimezone(IST)
            with self._store.transaction() as st:
                today = now.date().isoformat()
                if st.kill_active:
                    return True
                if st.halted_day == today and self.cfg.limits.square_off_on_daily_loss:
                    return True
            return (now.date().weekday() in self.cfg.window.weekdays
                    and now.time() >= self.cfg.window.force_square_off)

    def add_listener(self, fn: Callable[[Signal, Decision], None]) -> None:
        """Called after EVERY decision (journal, alerts)."""
        self._listeners.append(fn)

    # ------------------------------------------------------------ evaluate
    def evaluate(self, signal: Signal) -> Decision:
        events: list[tuple[str, str]] = []
        with self._lock:
            try:
                now = self._clock().astimezone(IST)
                with self._store.transaction() as st:
                    decision = self._evaluate(signal, now, st, events)
            except Exception as e:
                log.exception("Risk engine error while evaluating %s", getattr(signal, "idempotency_key", "?"))
                decision = Decision(False, (Rejection(Code.INTERNAL_ERROR, f"{type(e).__name__}: {e}"),), None)
        self._publish(signal, decision, events)
        return decision

    def _evaluate(self, s: Signal, now: datetime, st: RiskState, events: list) -> Decision:
        today = now.date()
        self._roll_day(st, today)
        self._prune_pending(st, now.timestamp())
        rej: list[Rejection] = []

        if s.idempotency_key in st.keys:
            rej.append(Rejection(Code.DUPLICATE, f"idempotency key {s.idempotency_key!r} was already approved on {st.keys[s.idempotency_key][:10]}"))

        assessment: RiskAssessment | None = None
        pend_key: str | None = None
        if s.kind == "ENTRY":
            assessment = self._entry_checks(s, now, st, rej, events)
        else:
            pend_key = self._exit_checks(s, now, st, rej)

        if rej:
            return Decision(False, tuple(rej), None)

        # ---- approve: record everything atomically with the decision
        st.keys[s.idempotency_key] = now.isoformat()
        if s.kind == "ENTRY":
            st.orders_today += len(s.legs)
            st.pending_entries[s.idempotency_key] = now.timestamp()
        else:
            st.pending_exits[pend_key or s.idempotency_key] = now.timestamp()
        order_id = uuid.uuid4().hex
        order = ApprovedOrder(order_id, s, now, assessment or RiskAssessment(None, None, None, None),
                              _seal(order_id, s, now))
        return Decision(True, (), order)

    # ------------------------------------------------------- entry checks
    def _entry_checks(self, s: Signal, now: datetime, st: RiskState, rej: list[Rejection], events: list) -> RiskAssessment | None:
        c, today, t = self.cfg, now.date(), now.time()

        if st.kill_active:
            rej.append(Rejection(Code.KILL_SWITCH, f"kill switch is ON ({st.kill_reason}); an operator must reset it"))

        if st.halted_day != today.isoformat():
            pnl = self._pnl()
            if pnl <= -c.limits.max_loss_per_day:
                self._halt_day(st, today, pnl, events)
        if st.halted_day == today.isoformat():
            rej.append(Rejection(Code.DAY_HALTED, f"trading halted for today: {st.halt_reason}"))

        h = self._feed_health()
        if not h.healthy:
            rej.append(Rejection(Code.FEED_UNHEALTHY, f"market data not trustworthy: {h.reason}"))

        if today.weekday() not in c.window.weekdays:
            rej.append(Rejection(Code.OUTSIDE_WINDOW, f"{today:%A} is not a trading day in risk.yaml"))
        elif t < c.window.trading_start:
            rej.append(Rejection(Code.OUTSIDE_WINDOW, f"{t:%H:%M} is before the trading window opens at {c.window.trading_start:%H:%M}"))
        elif t > min(c.window.no_new_entries_after, c.window.trading_end):
            rej.append(Rejection(Code.OUTSIDE_WINDOW, f"{t:%H:%M} is after the last time for new entries ({min(c.window.no_new_entries_after, c.window.trading_end):%H:%M})"))

        expiry_day = any(l.expiry == today for l in s.legs)
        if expiry_day and c.expiry_day.block_new_entries:
            rej.append(Rejection(Code.EXPIRY_DAY_BLOCKED, "new entries are blocked on expiry day (risk.yaml)"))

        # ---- structure (rule 4)
        cls = classify(s.legs)
        kind = cls.kind
        if kind is None:
            rej.append(Rejection(Code.NOT_DEFINED_RISK, cls.reason))
        elif kind not in c.structures.allowed:
            rej.append(Rejection(Code.STRUCTURE_DISABLED, f"{kind} is recognised but not enabled in risk.yaml (allowed: {c.structures.allowed})"))

        # ---- quotes, spread
        prices: dict[str, float] = {}
        quotes_ok = True
        for leg in s.legs:
            q = self._quotes(leg.token)
            if q is None:
                rej.append(Rejection(Code.NO_QUOTE, f"no market quote for {leg.symbol}")); quotes_ok = False; continue
            if now.timestamp() - q.received_at > c.quotes.max_quote_age_seconds:
                rej.append(Rejection(Code.STALE_QUOTE, f"quote for {leg.symbol} is {now.timestamp() - q.received_at:.0f}s old (limit {c.quotes.max_quote_age_seconds:g}s)")); quotes_ok = False; continue
            if q.bid is None or q.ask is None or q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
                rej.append(Rejection(Code.SPREAD_UNKNOWN, f"no usable bid/ask for {leg.symbol}; cannot judge the spread")); quotes_ok = False; continue
            spread = q.ask - q.bid
            pct = spread / ((q.ask + q.bid) / 2) * 100
            if pct > c.quotes.max_spread_pct or spread > c.quotes.max_spread_abs:
                rej.append(Rejection(Code.SPREAD_TOO_WIDE, f"{leg.symbol} spread {spread:.2f} ({pct:.1f}%) exceeds limits ({c.quotes.max_spread_abs:g} / {c.quotes.max_spread_pct:g}%)"))
                quotes_ok = False
            # Pessimistic fills: pay the ask, receive the bid.
            prices[leg.token] = q.ask if leg.side == "BUY" else q.bid

        # ---- size and counts
        cap = c.limits.max_lots_per_order
        if expiry_day:
            cap = min(cap, c.expiry_day.max_lots_per_order)
        biggest = max(l.lots for l in s.legs)
        if biggest > cap:
            rej.append(Rejection(Code.TOO_MANY_LOTS, f"{biggest} lots in one leg exceeds the cap of {cap}" + (" (expiry-day cap)" if expiry_day and cap < c.limits.max_lots_per_order else "")))

        open_now = len(self._portfolio.open_positions()) + len(st.pending_entries)
        if open_now >= c.limits.max_open_positions:
            rej.append(Rejection(Code.MAX_POSITIONS, f"{open_now} positions open or in flight; limit is {c.limits.max_open_positions}"))
        if st.orders_today + len(s.legs) > c.limits.max_orders_per_day:
            rej.append(Rejection(Code.MAX_ORDERS, f"{st.orders_today} order legs used today + {len(s.legs)} would exceed the daily limit of {c.limits.max_orders_per_day}"))

        # ---- stop-loss (mandatory) and loss limits
        if s.stop_loss is None:
            rej.append(Rejection(Code.NO_STOP_LOSS, "no stop-loss: every position must have one"))
        if kind is None or not quotes_ok:
            return None  # can't value the structure; the reasons above already say why
        try:
            val = value_structure(kind, s.legs, lambda l: prices[l.token])
        except StructureError as e:
            rej.append(Rejection(Code.BAD_PRICING, str(e)))
            return None
        qty = s.legs[0].qty  # equal lots across legs is guaranteed by classify()
        max_loss = val.max_loss * qty
        if max_loss > c.limits.max_structure_loss:
            rej.append(Rejection(Code.MAX_STRUCTURE_LOSS, f"worst-case loss {max_loss:,.0f} exceeds the limit of {c.limits.max_structure_loss:,.0f}"))
        loss_at_stop = None
        if s.stop_loss is not None:
            try:
                loss_at_stop = check_stop_and_target(val, s.stop_loss, s.target) * qty
            except StructureError as e:
                rej.append(Rejection(Code.BAD_STOP_OR_TARGET, str(e)))
            else:
                if loss_at_stop > c.limits.max_loss_per_trade:
                    rej.append(Rejection(Code.MAX_LOSS_PER_TRADE, f"loss at the stop-loss would be {loss_at_stop:,.0f}; limit is {c.limits.max_loss_per_trade:,.0f}"))
        return RiskAssessment(kind, val.net_debit, loss_at_stop, max_loss)

    # -------------------------------------------------------- exit checks
    def _exit_checks(self, s: Signal, now: datetime, st: RiskState, rej: list[Rejection]) -> str:
        positions = self._portfolio.open_positions()
        if s.position_id is not None:
            positions = [p for p in positions if p.position_id == s.position_id]
            if not positions:
                rej.append(Rejection(Code.UNKNOWN_POSITION, f"no open position with id {s.position_id!r}"))

        # Net holdings per contract: BUY legs are long (+lots), SELL legs short (-lots).
        held: dict[tuple, int] = {}
        for p in positions:
            for l in p.legs:
                k = (l.underlying, l.expiry, l.strike, l.opt_type)
                held[k] = held.get(k, 0) + (l.lots if l.side == "BUY" else -l.lots)
        held_before = dict(held)
        for l in s.legs:
            k = (l.underlying, l.expiry, l.strike, l.opt_type)
            have = held.get(k, 0)
            if l.side == "SELL":  # closing a long
                ok = have >= l.lots
                held[k] = have - l.lots
            else:  # buying back a short
                ok = -have >= l.lots
                held[k] = have + l.lots
            if not ok and positions:
                rej.append(Rejection(Code.EXIT_NOT_COVERED, f"{l.side} {l.lots} x {l.symbol} would open or enlarge a position, not close one"))
            elif not ok and s.position_id is None:
                rej.append(Rejection(Code.EXIT_NOT_COVERED, f"{l.side} {l.lots} x {l.symbol}: there is no open position to close"))

        # Rule 4 again: closing legs in the wrong combination (e.g. selling the
        # protective wing of a credit spread) would leave a short option naked.
        if not any(r.code in (Code.EXIT_NOT_COVERED, Code.UNKNOWN_POSITION) for r in rej):
            before = coverage_deficit(held_before)
            for g, n in coverage_deficit(held).items():
                if n > before.get(g, 0):
                    rej.append(Rejection(Code.EXIT_NOT_COVERED, f"this exit would leave {n} short {g[2]} lot(s) on {g[0]} {g[1]} uncovered (naked); close the short leg first or together"))

        pend_key = exit_key(s)
        if pend_key in st.pending_exits:
            age = now.timestamp() - st.pending_exits[pend_key]
            rej.append(Rejection(Code.EXIT_IN_FLIGHT, f"an exit for {pend_key} was approved {age:.0f}s ago and has not completed"))
        return pend_key

    # --------------------------------------------------------- state bits
    @staticmethod
    def _roll_day(st: RiskState, today: date) -> None:
        if st.day != today.isoformat():
            st.day = today.isoformat()
            st.orders_today = 0
            cutoff = today - timedelta(days=7)
            st.keys = {k: v for k, v in st.keys.items() if datetime.fromisoformat(v).date() >= cutoff}

    def _prune_pending(self, st: RiskState, now_epoch: float) -> None:
        ttl = self.cfg.inflight.pending_ttl_seconds
        st.pending_entries = {k: v for k, v in st.pending_entries.items() if now_epoch - v <= ttl}
        st.pending_exits = {k: v for k, v in st.pending_exits.items() if now_epoch - v <= ttl}

    def _pnl(self) -> float:
        p = self._portfolio
        pnl = p.realised_pnl_today() + p.unrealised_pnl()
        if not math.isfinite(pnl):
            raise ValueError("portfolio returned a non-finite P&L")
        return pnl

    def _halt_day(self, st: RiskState, today: date, pnl: float, events: list) -> None:
        st.halted_day = today.isoformat()
        st.halt_reason = f"day P&L {pnl:,.0f} reached the loss limit of -{self.cfg.limits.max_loss_per_day:,.0f}"
        st.halt_actioned = not self.cfg.limits.square_off_on_daily_loss
        log.critical("DAILY LOSS LIMIT: %s", st.halt_reason)
        events.append(("CRITICAL", f"Daily loss limit hit: {st.halt_reason}. No more entries today."
                       + (" Closing open positions." if self.cfg.limits.square_off_on_daily_loss else "")))

    # ------------------------------------------------- completion callbacks
    def entry_settled(self, idempotency_key: str) -> None:
        """Execution calls this when an approved entry has filled or failed."""
        with self._lock, self._store.transaction() as st:
            st.pending_entries.pop(idempotency_key, None)

    def exit_settled(self, position_id: str) -> None:
        """Execution calls this when an exit has filled or failed (allows a retry)."""
        with self._lock, self._store.transaction() as st:
            st.pending_exits.pop(position_id, None)

    # -------------------------------------------------------- publishing
    def _publish(self, signal: Signal, decision: Decision, events: list) -> None:
        key = getattr(signal, "idempotency_key", "?")
        if decision.approved:
            log.info("APPROVED %s %s key=%s %s", signal.kind, signal.strategy, key, decision.order.assessment)  # type: ignore[union-attr]
        else:
            log.warning("REJECTED %s %s key=%s: %s", getattr(signal, "kind", "?"), getattr(signal, "strategy", "?"), key, decision.summary())
        for level, msg in events:
            self._notify(level, msg)
        for fn in list(self._listeners):
            try:
                fn(signal, decision)
            except Exception:
                log.exception("Risk listener %r failed", getattr(fn, "__name__", fn))

    def _notify(self, level: str, message: str) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier(level, message)
        except Exception:
            log.exception("Risk notifier failed")

    # ============================================================ kill switch
    def kill(self, reason: str, source: str = "operator") -> None:
        """Stop everything: block entries, cancel pending orders, close all positions."""
        with self._lock:
            now = self._clock()
            with self._store.transaction() as st:
                st.kill_active, st.kill_reason, st.kill_source = True, reason, source
                st.kill_at, st.kill_actioned = now.isoformat(), False
            log.critical("KILL SWITCH ON (%s): %s", source, reason)
            self._notify("CRITICAL", f"KILL SWITCH ON by {source}: {reason}")
            self.run_pending_actions()

    def reset_kill(self, operator: str, note: str) -> None:
        """Manual re-enable. Requires who and why, which are logged."""
        if not operator.strip() or not note.strip():
            raise ValueError("reset needs an operator name and a note saying why it is safe")
        with self._lock, self._store.transaction() as st:
            was = st.kill_active
            st.kill_active, st.kill_actioned = False, True
        log.critical("KILL SWITCH RESET by %s: %s", operator, note)
        if was:
            self._notify("WARNING", f"Kill switch RESET by {operator}: {note}")

    # ============================================================== polling
    def poll(self) -> None:
        """Call about once a second from the scheduler."""
        with self._lock:
            self.check_daily_loss()
            self.run_pending_actions()

    def check_daily_loss(self) -> bool:
        """Trip the daily halt if the limit is reached. Returns True if halted today."""
        events: list = []
        with self._lock:
            today = self._clock().astimezone(IST).date()
            with self._store.transaction() as st:
                if st.halted_day != today.isoformat():
                    pnl = self._pnl()
                    if pnl <= -self.cfg.limits.max_loss_per_day:
                        self._halt_day(st, today, pnl, events)
                halted = st.halted_day == today.isoformat()
            for level, msg in events:
                self._notify(level, msg)
            return halted

    def run_pending_actions(self) -> None:
        """Carry out owed kill / daily-halt / end-of-day actions, retrying on failure.

        Never runs inside a state transaction: the actions submit exits through
        `evaluate`, which takes the same file lock.
        """
        with self._lock:
            now = self._clock().astimezone(IST)
            today = now.date().isoformat()
            with self._store.transaction() as st:
                kill_due = st.kill_active and not st.kill_actioned
                halt_due = st.halted_day == today and not st.halt_actioned
                eod_due = (now.date().weekday() in self.cfg.window.weekdays
                           and now.time() >= self.cfg.window.force_square_off and st.eod_day != today)
            if eod_due and not self._portfolio.open_positions():
                eod_due = False  # nothing to close
            if not (kill_due or halt_due or eod_due):
                return
            if self._actions is None:
                log.error("Actions are due (kill=%s halt=%s eod=%s) but no broker actions are wired", kill_due, halt_due, eod_due)
                return
            try:
                if kill_due or halt_due:
                    self._actions.cancel_all_pending()
                self._actions.square_off_all()
            except Exception as e:
                log.exception("Emergency actions FAILED; will retry")
                if time_mod.time() - self._last_action_fail_notice > 60:
                    self._last_action_fail_notice = time_mod.time()
                    self._notify("CRITICAL", f"Square-off/cancel FAILED ({type(e).__name__}: {e}). Retrying. CHECK POSITIONS MANUALLY.")
                return
            with self._store.transaction() as st:
                if kill_due:
                    st.kill_actioned = True
                if halt_due:
                    st.halt_actioned = True
                if eod_due:
                    st.eod_day = today
            if eod_due and not (kill_due or halt_due):
                self._notify("WARNING", "Positions were still open at the forced square-off time; closing them.")

    # ================================================================ status
    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock().astimezone(IST)
            with self._store.transaction() as st:
                pos = len(self._portfolio.open_positions())
                return {
                    "kill_active": st.kill_active, "kill_reason": st.kill_reason, "kill_source": st.kill_source,
                    "day_halted": st.halted_day == now.date().isoformat(), "halt_reason": st.halt_reason,
                    "orders_used": st.orders_today if st.day == now.date().isoformat() else 0,
                    "orders_limit": self.cfg.limits.max_orders_per_day,
                    "open_positions": pos, "positions_limit": self.cfg.limits.max_open_positions,
                    "pnl_today": self._pnl(), "loss_limit": self.cfg.limits.max_loss_per_day,
                }


# ====================================================================== CLI

def _cli(argv: list[str] | None = None) -> int:
    """Operator commands that work even when the trading app is not running.

        python -m trader.risk status
        python -m trader.risk kill --reason "why"
        python -m trader.risk reset --operator "Name" --note "why it is safe"

    `kill` flips the persistent switch immediately; the running app notices within
    a second and cancels orders / closes positions. If the app is down, the switch
    simply stays ON until reset.
    """
    ap = argparse.ArgumentParser(prog="python -m trader.risk")
    ap.add_argument("--config", default="config/risk.yaml")
    ap.add_argument("--state", help="override the state file path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    k = sub.add_parser("kill"); k.add_argument("--reason", default="manual kill from command line")
    r = sub.add_parser("reset")
    r.add_argument("--operator", required=True); r.add_argument("--note", required=True)
    r.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    args = ap.parse_args(argv)

    store = RiskStateStore(args.state or load_risk_config(args.config).state_file)
    if args.cmd == "status":
        with store.transaction() as st:
            print(json.dumps({k: v for k, v in asdict(st).items() if k not in ("keys", "pending_entries", "pending_exits")}, indent=2))
        return 0
    if args.cmd == "kill":
        with store.transaction() as st:
            st.kill_active, st.kill_reason, st.kill_source = True, args.reason, "cli"
            st.kill_at, st.kill_actioned = datetime.now(IST).isoformat(), False
        print("KILL SWITCH ON. New orders are blocked now; the running app will cancel orders and close positions within seconds.")
        return 0
    if not args.operator.strip() or not args.note.strip():
        print("operator and note must not be empty", file=sys.stderr); return 2
    if not args.yes and input("Type RESET to re-enable trading: ").strip() != "RESET":
        print("Not reset."); return 1
    with store.transaction() as st:
        st.kill_active, st.kill_actioned = False, True
    log.critical("KILL SWITCH RESET by %s: %s", args.operator, args.note)
    print(f"Kill switch reset by {args.operator}.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
