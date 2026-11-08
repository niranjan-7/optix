"""Recognise defined-risk option structures and compute their worst-case loss.

This enforces rule 4 (no naked option selling). The approach is a *whitelist by
shape*: a set of legs is only tradable if it matches one of four known shapes in
which every short option is fully covered by a long option of the same expiry and
type, so the maximum loss is finite and computable. Anything else - a lone short,
ratio spreads, calendars, short straddles/strangles, etc. - is "unrecognised" and
therefore rejected. We never try to be clever about new shapes.

Conventions
-----------
* Prices are per unit (per share of the underlying), not per lot.
* net_debit > 0  : we pay (debit structure).   net_debit < 0 : we receive (credit).
* Entry prices are pessimistic: BUY at the ask, SELL at the bid.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from .signals import Leg

LONG_OPTION = "LONG_OPTION"
VERTICAL_DEBIT = "VERTICAL_DEBIT"
VERTICAL_CREDIT = "VERTICAL_CREDIT"
IRON_CONDOR = "IRON_CONDOR"
ALL_KINDS = (LONG_OPTION, VERTICAL_DEBIT, VERTICAL_CREDIT, IRON_CONDOR)


class StructureError(ValueError):
    """The structure or its prices are not acceptable; message is operator-facing."""


@dataclass(frozen=True)
class Classification:
    kind: str | None  # None => not a recognised defined-risk shape
    reason: str = ""  # why not (when kind is None)


def _unsupported(reason: str) -> Classification:
    return Classification(None, reason)


def _vertical_kind(buy: Leg, sell: Leg) -> str:
    """Debit if we buy the nearer-the-money strike, credit if we sell it."""
    if buy.opt_type == "CE":
        return VERTICAL_DEBIT if buy.strike < sell.strike else VERTICAL_CREDIT
    return VERTICAL_DEBIT if buy.strike > sell.strike else VERTICAL_CREDIT


def classify(legs: Sequence[Leg]) -> Classification:
    n = len(legs)
    if n == 1:
        leg = legs[0]
        if leg.side == "BUY":
            return Classification(LONG_OPTION)
        return _unsupported(
            f"naked short {leg.opt_type}: selling an option without owning cover has "
            "open-ended risk, which is never allowed")

    if n == 2:
        a, b = legs
        if (a.underlying, a.expiry) != (b.underlying, b.expiry):
            return _unsupported("legs differ in underlying or expiry (calendar/diagonal spreads are not supported)")
        if a.opt_type != b.opt_type:
            return _unsupported("one call + one put is a straddle/strangle, not a vertical spread; not supported")
        if {a.side, b.side} != {"BUY", "SELL"}:
            return _unsupported("a two-leg spread needs one BUY and one SELL")
        if a.lots != b.lots:
            return _unsupported("unequal lots (ratio spread) would leave part of the short leg uncovered")
        if a.strike == b.strike:
            return _unsupported("both legs have the same strike")
        buy, sell = (a, b) if a.side == "BUY" else (b, a)
        return Classification(_vertical_kind(buy, sell))

    if n == 4:
        if len({(l.underlying, l.expiry) for l in legs}) != 1:
            return _unsupported("iron condor legs must share underlying and expiry")
        if len({l.lots for l in legs}) != 1:
            return _unsupported("iron condor legs must all have the same lots")
        puts = [l for l in legs if l.opt_type == "PE"]
        calls = [l for l in legs if l.opt_type == "CE"]
        if len(puts) != 2 or len(calls) != 2:
            return _unsupported("an iron condor is exactly 2 puts + 2 calls")
        for side_legs in (puts, calls):
            if {l.side for l in side_legs} != {"BUY", "SELL"}:
                return _unsupported("each wing of an iron condor needs one BUY and one SELL")
            if side_legs[0].strike == side_legs[1].strike:
                return _unsupported("a wing has two legs at the same strike")
        sp = next(l for l in puts if l.side == "SELL")
        lp = next(l for l in puts if l.side == "BUY")
        sc = next(l for l in calls if l.side == "SELL")
        lc = next(l for l in calls if l.side == "BUY")
        if not (lp.strike < sp.strike < sc.strike < lc.strike):
            return _unsupported(
                "strikes must run long put < short put < short call < long call; "
                "anything else is not a standard iron condor")
        return Classification(IRON_CONDOR)

    return _unsupported(f"{n}-leg combination is not one of the supported shapes")


@dataclass(frozen=True)
class Valuation:
    kind: str
    net_debit: float  # per unit; negative = credit received
    max_loss: float  # per unit, worst case


def value_structure(kind: str, legs: Sequence[Leg], entry_price: Callable[[Leg], float]) -> Valuation:
    """Net premium and worst-case loss per unit. Raises StructureError on bad prices."""
    prices = [entry_price(l) for l in legs]
    if any((not math.isfinite(p)) or p <= 0 for p in prices):
        raise StructureError("a leg has a missing or non-positive price")
    net_debit = sum(p if l.side == "BUY" else -p for l, p in zip(legs, prices))

    if kind == LONG_OPTION:
        return Valuation(kind, net_debit, max_loss=net_debit)

    if kind == VERTICAL_DEBIT:
        if net_debit <= 0:
            raise StructureError(f"debit spread priced at a non-positive debit ({net_debit:.2f}); bad quotes")
        return Valuation(kind, net_debit, max_loss=net_debit)

    if kind == VERTICAL_CREDIT:
        width = abs(legs[0].strike - legs[1].strike)
        credit = -net_debit
        if not (0 < credit < width):
            raise StructureError(f"credit {credit:.2f} must be between 0 and the spread width {width:g}; bad quotes")
        return Valuation(kind, net_debit, max_loss=width - credit)

    if kind == IRON_CONDOR:
        puts = sorted((l for l in legs if l.opt_type == "PE"), key=lambda l: l.strike)
        calls = sorted((l for l in legs if l.opt_type == "CE"), key=lambda l: l.strike)
        # Spot can only be beyond ONE wing at expiry, so only the wider wing can lose.
        width = max(puts[1].strike - puts[0].strike, calls[1].strike - calls[0].strike)
        credit = -net_debit
        if not (0 < credit < width):
            raise StructureError(f"credit {credit:.2f} must be between 0 and the widest wing {width:g}; bad quotes")
        return Valuation(kind, net_debit, max_loss=width - credit)

    raise StructureError(f"unknown structure kind {kind!r}")


def check_stop_and_target(v: Valuation, stop_loss: float | None, target: float | None) -> float:
    """Validate the stop (mandatory) and target (optional); return loss per unit AT the stop.

    stop_loss / target are the structure's net price per unit:
      * debit structure : exit when its value falls to stop_loss (0 < stop < debit);
                          target is a higher value (target > debit).
      * credit structure: exit when the cost to close rises to stop_loss
                          (credit < stop < width);  target is a lower cost (0 <= target < credit).
    """
    if stop_loss is None:
        raise StructureError("no stop-loss: every position must have one")
    if not math.isfinite(stop_loss):
        raise StructureError("stop-loss is not a valid number")

    if v.net_debit > 0:  # debit structure
        if not (0 < stop_loss < v.net_debit):
            raise StructureError(
                f"stop-loss {stop_loss:g} must be above 0 and below the entry price {v.net_debit:.2f}")
        if target is not None and not (math.isfinite(target) and target > v.net_debit):
            raise StructureError(f"target {target:g} must be above the entry price {v.net_debit:.2f}")
        return v.net_debit - stop_loss

    credit = -v.net_debit
    width = credit + v.max_loss
    if not (credit < stop_loss < width):
        raise StructureError(
            f"stop-loss {stop_loss:g} must be above the credit {credit:.2f} and below the "
            f"maximum cost {width:g}; a stop at or beyond max loss is not a stop")
    if target is not None and not (math.isfinite(target) and 0 <= target < credit):
        raise StructureError(f"target {target:g} must be between 0 and the credit {credit:.2f}")
    return stop_loss - credit


def coverage_deficit(holdings: dict[tuple, int]) -> dict[tuple, int]:
    """Short lots NOT covered by long lots, per (underlying, expiry, option type).

    `holdings` maps (underlying, expiry, strike, opt_type) -> signed lots
    (+ long, - short). For one expiry and option type, payoff at expiry is
    bounded on the downside exactly when total long lots >= total short lots,
    whatever the strikes (the long options' slope offsets the shorts'). A long
    in a DIFFERENT expiry does not cover a short (calendar risk), so groups are
    kept apart. Used to stop an "exit" from stripping the protection off a
    short leg and leaving it naked.
    """
    longs: dict[tuple, int] = {}
    shorts: dict[tuple, int] = {}
    for (und, exp, _strike, typ), lots in holdings.items():
        g = (und, exp, typ)
        if lots > 0:
            longs[g] = longs.get(g, 0) + lots
        elif lots < 0:
            shorts[g] = shorts.get(g, 0) - lots
    return {g: n - longs.get(g, 0) for g, n in shorts.items() if n > longs.get(g, 0)}
