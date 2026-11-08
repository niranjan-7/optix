from datetime import date

import pytest

from trader.signals import Leg
from trader.structures import (
    IRON_CONDOR, LONG_OPTION, VERTICAL_CREDIT, VERTICAL_DEBIT, StructureError,
    check_stop_and_target, classify, value_structure,
)

EXP = date(2026, 11, 17)


def L(side, strike, typ="CE", lots=1, exp=EXP, und="NIFTY"):
    return Leg(und, exp, strike, typ, side, lots, 65, f"{und}{strike}{typ}", f"{strike}{typ}")


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("legs,kind", [
    ([L("BUY", 25000)], LONG_OPTION),
    ([L("BUY", 25000, "PE")], LONG_OPTION),
    # calls: buy lower strike / sell higher = bull call (debit); the reverse = bear call (credit)
    ([L("BUY", 25000), L("SELL", 25100)], VERTICAL_DEBIT),
    ([L("SELL", 25000), L("BUY", 25100)], VERTICAL_CREDIT),
    # puts: buy higher / sell lower = bear put (debit); sell higher / buy lower = bull put (credit)
    ([L("BUY", 25000, "PE"), L("SELL", 24900, "PE")], VERTICAL_DEBIT),
    ([L("SELL", 25000, "PE"), L("BUY", 24900, "PE")], VERTICAL_CREDIT),
    # order of legs must not matter
    ([L("SELL", 25100), L("BUY", 25000)], VERTICAL_DEBIT),
    ([L("BUY", 24800, "PE"), L("SELL", 24900, "PE"), L("SELL", 25100), L("BUY", 25200)], IRON_CONDOR),
    ([L("BUY", 25200), L("SELL", 25100), L("SELL", 24900, "PE"), L("BUY", 24700, "PE")], IRON_CONDOR),  # uneven wings
])
def test_recognised_shapes(legs, kind):
    assert classify(legs).kind == kind


@pytest.mark.parametrize("legs,why", [
    ([L("SELL", 25000)], "naked short"),
    ([L("SELL", 25000, "PE")], "naked short"),
    ([L("SELL", 25000), L("SELL", 25000, "PE")], "straddle"),  # short straddle: two nakeds
    ([L("SELL", 25000), L("SELL", 25100)], "one BUY and one SELL"),
    ([L("BUY", 25000), L("BUY", 25100)], "one BUY and one SELL"),
    ([L("BUY", 25000), L("SELL", 25000, "PE")], "straddle"),
    ([L("BUY", 25000, lots=1), L("SELL", 25100, lots=2)], "ratio"),  # 1x2 ratio: half the short is naked
    ([L("BUY", 25000), L("SELL", 25000)], "same strike"),
    ([L("BUY", 25000), L("SELL", 25100, exp=date(2026, 11, 24))], "calendar"),
    ([L("BUY", 25000), L("SELL", 25100, und="BANKNIFTY")], "calendar"),
    ([L("BUY", 24800, "PE"), L("SELL", 24900, "PE"), L("SELL", 25100)], "supported shapes"),  # 3 legs
    # four legs but short strikes crossed (short put above short call) -> not a condor
    ([L("BUY", 24800, "PE"), L("SELL", 25200, "PE"), L("SELL", 25100), L("BUY", 25300)], "long put < short put"),
    # a "condor" whose call wing is a DEBIT vertical (buy near, sell far): short far call is uncovered
    ([L("BUY", 24800, "PE"), L("SELL", 24900, "PE"), L("BUY", 25100), L("SELL", 25200)], "long put < short put"),
    # four legs, equal total but different lots
    ([L("BUY", 24800, "PE"), L("SELL", 24900, "PE", lots=2), L("SELL", 25100), L("BUY", 25200)], "same lots"),
    ([L("BUY", 24800, "PE"), L("SELL", 24900, "PE"), L("SELL", 25100, "PE"), L("BUY", 25200, "PE")], "2 puts + 2 calls"),
])
def test_unsupported_shapes_are_rejected_with_reason(legs, why):
    c = classify(legs)
    assert c.kind is None and why in c.reason


def test_every_short_leg_in_any_recognised_structure_is_covered():
    """Property check: enumerate strike combos; if classify() accepts, shorts must be covered."""
    strikes = [24800, 24900, 25000, 25100, 25200]
    import itertools
    accepted = 0
    for a, b in itertools.product(strikes, strikes):
        for typ in ("CE", "PE"):
            for sa, sb in itertools.product(("BUY", "SELL"), repeat=2):
                legs = [L(sa, a, typ), L(sb, b, typ)]
                k = classify(legs).kind
                if k is None:
                    continue
                accepted += 1
                short = next(l for l in legs if l.side == "SELL")
                long = next(l for l in legs if l.side == "BUY")
                # cover: a CE short needs a long CE at a HIGHER strike (or lower strike = it's a debit, fully paid);
                # what matters is max loss is finite - verified by value_structure below
                v = value_structure(k, legs, lambda l: 50.0 if l.side == "BUY" else 80.0) if k == VERTICAL_CREDIT else None
                if v is not None:
                    assert v.max_loss == pytest.approx(abs(a - b) - 30.0) and v.max_loss > 0
                assert short.strike != long.strike
    assert accepted > 0


# --------------------------------------------------------------- valuation

def price_map(d):
    return lambda leg: d[(leg.strike, leg.opt_type, leg.side)]


def test_long_option_value():
    v = value_structure(LONG_OPTION, [L("BUY", 25000)], lambda l: 100.25)
    assert (v.net_debit, v.max_loss) == (100.25, 100.25)


def test_debit_spread_value():
    legs = [L("BUY", 25000), L("SELL", 25100)]
    v = value_structure(VERTICAL_DEBIT, legs, price_map({(25000, "CE", "BUY"): 110.0, (25100, "CE", "SELL"): 60.0}))
    assert v.net_debit == 50.0 and v.max_loss == 50.0


def test_credit_spread_value_and_max_loss_is_width_minus_credit():
    legs = [L("SELL", 25000), L("BUY", 25100)]
    v = value_structure(VERTICAL_CREDIT, legs, price_map({(25000, "CE", "SELL"): 110.0, (25100, "CE", "BUY"): 60.0}))
    assert v.net_debit == -50.0 and v.max_loss == 50.0  # width 100 - credit 50


def test_iron_condor_max_loss_uses_widest_wing():
    legs = [L("BUY", 24700, "PE"), L("SELL", 24900, "PE"), L("SELL", 25100), L("BUY", 25200)]  # put wing 200, call wing 100
    px = {(24700, "PE", "BUY"): 10, (24900, "PE", "SELL"): 40, (25100, "CE", "SELL"): 45, (25200, "CE", "BUY"): 15}
    v = value_structure(IRON_CONDOR, legs, price_map(px))
    assert v.net_debit == -60.0 and v.max_loss == 140.0  # 200 - 60


@pytest.mark.parametrize("kind,legs,prices", [
    (LONG_OPTION, [L("BUY", 25000)], lambda l: 0.0),
    (LONG_OPTION, [L("BUY", 25000)], lambda l: float("nan")),
    (VERTICAL_DEBIT, [L("BUY", 25000), L("SELL", 25100)], price_map({(25000, "CE", "BUY"): 50.0, (25100, "CE", "SELL"): 60.0})),  # negative debit
    (VERTICAL_CREDIT, [L("SELL", 25000), L("BUY", 25100)], price_map({(25000, "CE", "SELL"): 50.0, (25100, "CE", "BUY"): 60.0})),  # negative credit
    (VERTICAL_CREDIT, [L("SELL", 25000), L("BUY", 25100)], price_map({(25000, "CE", "SELL"): 190.0, (25100, "CE", "BUY"): 10.0})),  # credit > width
])
def test_bad_prices_raise(kind, legs, prices):
    with pytest.raises(StructureError):
        value_structure(kind, legs, prices)


# ----------------------------------------------------------- stop / target

def debit_val(d=100.0):
    return value_structure(LONG_OPTION, [L("BUY", 25000)], lambda l: d)


def credit_val():
    legs = [L("SELL", 25000), L("BUY", 25100)]
    return value_structure(VERTICAL_CREDIT, legs, price_map({(25000, "CE", "SELL"): 110.0, (25100, "CE", "BUY"): 60.0}))  # credit 50, width 100


def test_debit_stop_gives_loss_per_unit():
    assert check_stop_and_target(debit_val(100), 75.0, 140.0) == 25.0


@pytest.mark.parametrize("stop", [None, 0, -5, 100, 120, float("nan"), float("inf")])
def test_debit_bad_stops(stop):
    with pytest.raises(StructureError):
        check_stop_and_target(debit_val(100), stop, None)


@pytest.mark.parametrize("target", [100, 90, float("nan")])
def test_debit_bad_targets(target):
    with pytest.raises(StructureError):
        check_stop_and_target(debit_val(100), 75.0, target)


def test_credit_stop_is_cost_to_close():
    assert check_stop_and_target(credit_val(), 80.0, 10.0) == 30.0  # pay 80 to close what we sold for 50


@pytest.mark.parametrize("stop", [None, 50, 40, 100, 150])  # must be strictly between credit 50 and width 100
def test_credit_bad_stops(stop):
    with pytest.raises(StructureError):
        check_stop_and_target(credit_val(), stop, None)


def test_credit_bad_target():
    with pytest.raises(StructureError):
        check_stop_and_target(credit_val(), 80.0, 60.0)  # target worse than the credit received


# ------------------------------------------------------------ coverage_deficit
from trader.structures import coverage_deficit  # noqa: E402

def H(*items):
    return {("NIFTY", EXP, k, typ): lots for k, typ, lots in items}


def test_coverage_deficit_cases():
    assert coverage_deficit(H((25000, "CE", 1))) == {}                              # plain long
    assert coverage_deficit(H((25000, "CE", -1), (25100, "CE", 1))) == {}           # credit spread, covered
    assert coverage_deficit(H((25000, "CE", 1), (25100, "CE", -1))) == {}           # debit spread
    assert coverage_deficit(H((25000, "CE", -1))) == {("NIFTY", EXP, "CE"): 1}      # naked
    assert coverage_deficit(H((25000, "CE", -2), (25100, "CE", 1))) == {("NIFTY", EXP, "CE"): 1}   # ratio
    assert coverage_deficit(H((25000, "CE", -1), (24900, "PE", 1))) == {("NIFTY", EXP, "CE"): 1}   # a put does not cover a call
    other = {("NIFTY", date(2026, 11, 24), 25100, "CE"): 1, ("NIFTY", EXP, 25000, "CE"): -1}
    assert coverage_deficit(other) == {("NIFTY", EXP, "CE"): 1}                      # different expiry does not cover
    assert coverage_deficit({}) == {}
