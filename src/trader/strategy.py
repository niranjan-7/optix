"""Strategy plugin interface, YAML config, context and runner.

Rules for strategy authors (the "why" behind the shape of this module):
  * A strategy is a pure-ish function of ticks + a read-only context. It never
    sees a broker, so it *cannot* place an order — it can only propose a Signal,
    which the risk engine then accepts or rejects (rule 2).
  * Time comes from the tick, not the wall clock, so the identical code can be
    replayed over stored ticks in the backtest module.
  * Positions and "entries today" come from the portfolio, not from the
    strategy's own memory: a signal the risk engine rejected must not leave the
    strategy believing it holds a position.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .feed import Feed, Instr, Tick
from .greeks import IST
from .instruments import InstrumentMaster, Option
from .signals import Leg, PositionView, Signal

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ views

class PortfolioView(Protocol):
    def open_positions(self, strategy: str) -> list[PositionView]: ...
    def entries_today(self, strategy: str, day: date) -> int: ...


class StrategyContext(Protocol):
    """What a strategy may look at. Deliberately small."""
    future: Instr  # the underlying future (our price reference)

    def on_tick(self, tick: Tick) -> None: ...
    def latest(self, instr: Instr) -> Tick | None: ...
    def atm_option(self, opt_type: str) -> Option | None: ...
    def open_positions(self, strategy: str) -> list[PositionView]: ...
    def entries_today(self, strategy: str, day: date) -> int: ...


class LiveContext:
    """Context backed by the instrument master and the latest ticks seen."""

    def __init__(self, master: InstrumentMaster, name: str, expiry: date,
                 future: Instr, index: Instr | None, portfolio: PortfolioView):
        self._master, self._name, self._expiry = master, name, expiry
        self.future, self._index = future, index
        self._portfolio = portfolio
        self._latest: dict[Instr, Tick] = {}

    def on_tick(self, tick: Tick) -> None:
        self._latest[tick.instr] = tick

    def latest(self, instr: Instr) -> Tick | None:
        return self._latest.get(instr)

    def _spot(self) -> float | None:
        # Prefer spot for ATM; the future is a fine fallback (it only ever shifts
        # the pick by a strike, and ChainTracker's window covers that).
        for ins in (self._index, self.future):
            t = self._latest.get(ins) if ins else None
            if t:
                return t.ltp
        return None

    def atm_option(self, opt_type: str) -> Option | None:
        spot = self._spot()
        if spot is None:
            return None
        strike = self._master.atm_strike(self._name, self._expiry, spot)
        return self._master.find_option(self._name, self._expiry, strike, opt_type)

    def open_positions(self, strategy: str) -> list[PositionView]:
        return self._portfolio.open_positions(strategy)

    def entries_today(self, strategy: str, day: date) -> int:
        return self._portfolio.entries_today(strategy, day)


class InMemoryPortfolio:
    """STAND-IN for demos and tests only.

    The real portfolio (fills, partial fills, P&L) arrives with the execution
    module; this just records what we tell it, so strategies can be exercised now.
    """

    def __init__(self) -> None:
        self._positions: dict[str, PositionView] = {}
        self._entries: dict[tuple[str, date], int] = {}
        self._n = 0

    def open_positions(self, strategy: str) -> list[PositionView]:
        return [p for p in self._positions.values() if p.strategy == strategy]

    def entries_today(self, strategy: str, day: date) -> int:
        return self._entries.get((strategy, day), 0)

    def apply(self, signal: Signal, fill_price: float) -> None:
        if signal.kind == "ENTRY":
            self._n += 1
            pid = f"P{self._n}"
            self._positions[pid] = PositionView(pid, signal.strategy, signal.legs, fill_price,
                                                signal.stop_loss, signal.target, signal.created_at)
            key = (signal.strategy, signal.created_at.astimezone(IST).date())
            self._entries[key] = self._entries.get(key, 0) + 1
        else:
            self._positions.pop(signal.position_id or "", None)


# ------------------------------------------------------------ the interface

class Strategy(ABC):
    """Subclass this. Return a Signal to propose a trade, else None."""

    type_name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]

    def __init__(self, name: str, params: BaseModel):
        self.name = name
        self.params = params

    @abstractmethod
    def on_tick(self, tick: Tick, ctx: StrategyContext) -> Signal | None:
        """Called for EVERY tick on the feed (index, future and options)."""


STRATEGY_TYPES: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    STRATEGY_TYPES[cls.type_name] = cls
    return cls


# ------------------------------------------------------------------ config

class StrategyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    type: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    strategies: list[StrategyEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "StrategyConfig":
        names = [s.name for s in self.strategies]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate strategy names: {sorted(dupes)}")
        return self


class StrategyConfigError(ValueError):
    """Operator-friendly problem in strategy.yaml."""


def load_strategy_config(path: str | Path = "config/strategy.yaml") -> StrategyConfig:
    with open(path, encoding="utf-8") as f:
        return StrategyConfig(**(yaml.safe_load(f) or {}))


def build_strategies(config: StrategyConfig) -> list[Strategy]:
    """Instantiate enabled strategies; fail loudly on unknown types / bad params."""
    from . import example_momentum  # noqa: F401  (importing registers it)

    out: list[Strategy] = []
    for entry in config.strategies:
        if not entry.enabled:
            log.info("Strategy %s is disabled in strategy.yaml; skipping", entry.name)
            continue
        cls = STRATEGY_TYPES.get(entry.type)
        if cls is None:
            raise StrategyConfigError(
                f"Strategy {entry.name!r}: unknown type {entry.type!r}. Known types: {sorted(STRATEGY_TYPES)}")
        try:
            params = cls.Params(**entry.params)
        except ValueError as e:  # pydantic ValidationError is a ValueError
            raise StrategyConfigError(f"Strategy {entry.name!r}: bad params in strategy.yaml:\n{e}") from e
        out.append(cls(entry.name, params))
    return out


# ------------------------------------------------------------------ runner

class StrategyRunner:
    """Fans each tick out to every strategy and hands signals to `submit`.

    `submit` will be the risk engine's entry point. Nothing here knows about
    brokers. One misbehaving strategy is isolated and, after repeated crashes,
    switched off (loudly) rather than allowed to spam or kill the feed thread.
    """

    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, strategies: list[Strategy], ctx: StrategyContext,
                 submit: Callable[[Signal], None],
                 on_disabled: Callable[[Strategy], None] | None = None):
        self._strategies = list(strategies)
        self._ctx, self._submit, self._on_disabled = ctx, submit, on_disabled
        self._errors: dict[str, int] = {s.name: 0 for s in strategies}
        self.disabled: set[str] = set()

    def attach(self, feed: Feed) -> None:
        feed.on_tick(self.on_tick)

    def set_context(self, ctx: StrategyContext) -> None:
        """Swap the market view (the backtest does this at each new day/expiry)."""
        self._ctx = ctx

    def on_tick(self, tick: Tick) -> None:
        self._ctx.on_tick(tick)  # update the latest-quote cache BEFORE strategies look
        for strat in self._strategies:
            if strat.name in self.disabled:
                continue
            try:
                signal = strat.on_tick(tick, self._ctx)
                self._errors[strat.name] = 0
            except Exception:
                self._errors[strat.name] += 1
                log.exception("Strategy %s crashed on tick %s (%d in a row)",
                              strat.name, tick.instr, self._errors[strat.name])
                if self._errors[strat.name] >= self.MAX_CONSECUTIVE_ERRORS:
                    self.disabled.add(strat.name)
                    log.critical("Strategy %s DISABLED after %d consecutive errors",
                                 strat.name, self.MAX_CONSECUTIVE_ERRORS)
                    if self._on_disabled:
                        self._on_disabled(strat)
                continue
            if signal is None:
                continue
            log.info("SIGNAL %s %s key=%s: %s", signal.kind, signal.strategy,
                     signal.idempotency_key, signal.reason)
            try:
                self._submit(signal)
            except Exception:
                log.exception("Submitting signal %s failed", signal.idempotency_key)


if __name__ == "__main__":
    # Offline demo:  python -m trader.strategy
    # Replays a SYNTHETIC morning (flat, then a climb) through the configured
    # strategies. Option prices come from Black-76 at a fixed 14% IV. There is no
    # risk engine yet, so this stand-in accepts every signal. Illustration only.
    import math
    from datetime import timedelta

    from .greeks import DEFAULT_RATE, bs_price, time_to_expiry
    from .instruments import ensure_master
    from .logging_utils import setup_logging

    setup_logging(logging.WARNING)
    today = date.today()
    master = InstrumentMaster.load(ensure_master(), {"NIFTY"})
    expiry = master.nearest_expiry("NIFTY", today, skip_today=True)
    fut_i = Instr("NFO", master.nearest_future("NIFTY", today).token)
    idx_i = Instr("NSE", master.index_token("NIFTY"))
    portfolio = InMemoryPortfolio()
    ctx = LiveContext(master, "NIFTY", expiry, fut_i, idx_i, portfolio)
    prices: dict[str, float] = {}

    def submit(sig: Signal) -> None:
        when = sig.created_at.strftime("%H:%M:%S")
        leg = sig.legs[0]
        print(f"{when}  {sig.kind:<5} {leg.side:<4} {leg.symbol}  "
              + (f"SL {sig.stop_loss:g} / TP {sig.target:g}  " if sig.kind == "ENTRY" else "")
              + f"\n          why: {sig.reason}")
        portfolio.apply(sig, sig.ref_price if sig.kind == "ENTRY" else prices[leg.token])

    # Running via `-m` makes this file `__main__`, a *different* module object from the
    # `trader.strategy` that example_momentum registers itself into; use the latter.
    import trader.strategy as real
    runner = StrategyRunner(real.build_strategies(real.load_strategy_config()), ctx, submit)
    t = datetime(today.year, today.month, today.day, 9, 30, tzinfo=IST)
    end = t.replace(hour=10, minute=45)
    while t < end:
        mins = (t - t.replace(hour=9, minute=30)).total_seconds() / 60
        spot = 25000 + 2 * math.sin(mins) + 8 * max(0.0, min(mins - 15, 15))  # flat, climb 15-30 min, flat
        fut = spot + 30
        T = time_to_expiry(t, expiry)
        epoch = t.timestamp()
        quotes = [(idx_i, spot), (fut_i, fut)]
        for o in master.chain("NIFTY", expiry, spot, 5):
            px = max(0.05, bs_price(fut, o.strike, T, DEFAULT_RATE, 0.14, o.opt_type, b=0.0))
            prices[o.token] = px
            quotes.append((Instr("NFO", o.token), px))
        for ins, px in quotes:  # future last, as in the tests
            if ins != fut_i:
                runner.on_tick(Tick(ins.exchange, ins.token, px, px - 0.25, px + 0.25, None, None, epoch, epoch))
        runner.on_tick(Tick("NFO", fut_i.token, fut, fut - 0.25, fut + 0.25, None, None, epoch, epoch))
        t += timedelta(seconds=5)
    print("done; positions still open:", len(portfolio.open_positions("example_momentum")))
