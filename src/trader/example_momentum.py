"""EXAMPLE ONLY - NOT TESTED FOR PROFIT.

A deliberately simple template showing how a strategy plugs in. Do not read
anything into its parameters; they are placeholders. Copy this file to start a
new strategy, change `type_name`, `Params` and `on_tick`, and add an entry to
config/strategy.yaml.

Idea: if the NIFTY future has moved by at least `move_points` over the last
`lookback_minutes`, buy the ATM call (up-move) or put (down-move). Exit on
stop-loss, target, or the end-of-day square-off time. Long options only, so the
maximum loss is the premium paid (defined risk, rule 4).
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .feed import Instr, Tick
from .greeks import IST
from .signals import Leg, Signal
from .strategy import Strategy, StrategyContext, register

log = logging.getLogger(__name__)


def _tick_round(x: float) -> float:
    """Round to the NIFTY option tick (0.05)."""
    return round(round(x / 0.05) * 0.05, 2)


class ExampleMomentumParams(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in YAML must fail, not be ignored

    lookback_minutes: float = Field(5, gt=0)
    move_points: float = Field(30, gt=0)
    lots: int = Field(1, ge=1)
    stop_loss_pct: float = Field(25, gt=0, lt=100)
    target_pct: float = Field(40, gt=0)
    entry_start: time = time(9, 30)
    entry_end: time = time(14, 30)
    square_off: time = time(15, 15)
    max_entries_per_day: int = Field(2, ge=1)
    cooldown_minutes: float = Field(5, ge=0)
    skip_expiry_day: bool = True
    max_quote_age_seconds: float = Field(10, gt=0)
    # Must exceed execution's order_timeout_seconds (20), so we only ask again after the
    # previous exit attempt has finished; asking sooner is refused as EXIT_IN_FLIGHT.
    exit_retry_seconds: float = Field(25, gt=0)

    @field_validator("entry_start", "entry_end", "square_off", mode="before")
    @classmethod
    def _times_must_be_quoted(cls, v):
        # YAML reads unquoted 10:30 as the integer 630; pydantic would then
        # silently turn that into a wrong time. Refuse instead.
        if not isinstance(v, (str, time)):
            raise ValueError('write times as quoted strings, e.g. "09:30"')
        return v

    @model_validator(mode="after")
    def _order(self) -> "ExampleMomentumParams":
        if not (self.entry_start < self.entry_end < self.square_off):
            raise ValueError("need entry_start < entry_end < square_off")
        return self


@register
class ExampleMomentum(Strategy):
    type_name = "example_momentum"
    Params = ExampleMomentumParams
    params: ExampleMomentumParams

    def __init__(self, name: str, params: ExampleMomentumParams):
        super().__init__(name, params)
        self._hist: deque[tuple[float, float]] = deque()  # (epoch_seconds, future price)
        self._hist_day: date | None = None
        self._last_entry_signal: float | None = None  # epoch seconds
        self._last_exit_signal: dict[str, float] = {}  # position_id -> epoch seconds

    # ---------------------------------------------------------------- entry
    def on_tick(self, tick: Tick, ctx: StrategyContext) -> Signal | None:
        now = datetime.fromtimestamp(tick.received_at, IST)
        exit_signal = self._check_exits(tick, ctx, now)
        if exit_signal is not None:
            return exit_signal  # closing always takes priority over opening
        if tick.instr != ctx.future:
            return None
        return self._check_entry(tick, ctx, now)

    def _check_entry(self, tick: Tick, ctx: StrategyContext, now: datetime) -> Signal | None:
        p = self.params
        ts = tick.received_at

        # History is per trading day: yesterday's close must not look like a move.
        if self._hist_day != now.date():
            self._hist.clear()
            self._hist_day = now.date()
        self._hist.append((ts, tick.ltp))
        cutoff = ts - p.lookback_minutes * 60
        # Keep exactly one sample at-or-before the cutoff as the reference price.
        while len(self._hist) > 1 and self._hist[1][0] <= cutoff:
            self._hist.popleft()

        if not (p.entry_start <= now.time() <= p.entry_end):
            return None
        ref_ts, ref_price = self._hist[0]
        age = ts - ref_ts
        # Need (almost) the full lookback of history, but not a reference so old
        # that a data gap (feed outage) is masquerading as a price move.
        if age < 0.8 * p.lookback_minutes * 60 or age > 1.5 * p.lookback_minutes * 60:
            return None
        move = tick.ltp - ref_price
        if abs(move) < p.move_points:
            return None

        if ctx.open_positions(self.name):
            return None
        if ctx.entries_today(self.name, now.date()) >= p.max_entries_per_day:
            return None
        # Cooldown counts from the last *signal*, so a risk-engine rejection
        # can't make us re-fire on every single tick.
        if self._last_entry_signal is not None and ts - self._last_entry_signal < p.cooldown_minutes * 60:
            return None

        opt_type = "CE" if move > 0 else "PE"
        opt = ctx.atm_option(opt_type)
        if opt is None:
            return None
        if p.skip_expiry_day and opt.expiry == now.date():
            return None
        quote = ctx.latest(Instr("NFO", opt.token))
        if quote is None or ts - quote.received_at > p.max_quote_age_seconds:
            return None  # no fresh price for the contract => no idea of SL/target
        # Assume we pay the ask when we can see it (pessimistic), else last price.
        entry_ref = quote.ask if quote.ask is not None else quote.ltp

        leg = Leg(opt.name, opt.expiry, opt.strike, opt.opt_type, "BUY", p.lots, opt.lot_size, opt.symbol, opt.token)
        self._last_entry_signal = ts
        return Signal(
            # Minute resolution: a retry within the same minute is the same intent
            # (dropped as a duplicate); a fresh idea next minute gets a new key.
            idempotency_key=f"{self.name}:ENTRY:{opt.symbol}:{now:%Y%m%d%H%M}",
            strategy=self.name, kind="ENTRY", legs=(leg,), created_at=now,
            stop_loss=_tick_round(entry_ref * (1 - p.stop_loss_pct / 100)),
            target=_tick_round(entry_ref * (1 + p.target_pct / 100)),
            ref_price=entry_ref,
            reason=(f"future moved {move:+.1f} pts in {age / 60:.1f} min "
                    f"(threshold {p.move_points:g} in {p.lookback_minutes:g}); buy ATM {opt.opt_type} "
                    f"{opt.strike:g} @ ~{entry_ref:.2f}"),
        )

    # ----------------------------------------------------------------- exit
    def _check_exits(self, tick: Tick, ctx: StrategyContext, now: datetime) -> Signal | None:
        p = self.params
        for pos in ctx.open_positions(self.name):
            leg = pos.legs[0]  # this example only ever opens single-leg longs
            reason = None
            if now.time() >= p.square_off:
                reason = f"square-off time {p.square_off:%H:%M} reached"
            elif tick.instr == Instr("NFO", leg.token):
                if pos.stop_loss is not None and tick.ltp <= pos.stop_loss:
                    reason = f"stop-loss hit: {tick.ltp:.2f} <= {pos.stop_loss:.2f}"
                elif pos.target is not None and tick.ltp >= pos.target:
                    reason = f"target hit: {tick.ltp:.2f} >= {pos.target:.2f}"
            if reason is None:
                continue
            last = self._last_exit_signal.get(pos.position_id)
            if last is not None and tick.received_at - last < p.exit_retry_seconds:
                continue  # already asked a moment ago; don't flood while it processes
            self._last_exit_signal[pos.position_id] = tick.received_at
            return Signal(
                idempotency_key=f"{self.name}:EXIT:{pos.position_id}:{int(tick.received_at // p.exit_retry_seconds)}",
                strategy=self.name, kind="EXIT", position_id=pos.position_id,
                legs=tuple(l.opposite() for l in pos.legs), created_at=now,
                ref_price=tick.ltp if tick.instr == Instr("NFO", leg.token) else None,
                reason=reason,
            )
        return None
