from datetime import date, datetime

import pytest

from trader.greeks import (
    IST, GreeksError, analyse, bs_greeks, bs_price, implied_vol, intrinsic, time_to_expiry,
)

# Reference values: Hull's textbook example and the standard 100/100 case; both were
# also recomputed independently with math.erf before being hard-coded here.
HULL = dict(S=42, K=40, T=0.5, r=0.10, sigma=0.20)
STD = dict(S=100, K=100, T=1.0, r=0.05, sigma=0.20)


def test_known_prices():
    assert bs_price(**HULL, opt_type="CE") == pytest.approx(4.7594, abs=1e-4)
    assert bs_price(**HULL, opt_type="PE") == pytest.approx(0.8086, abs=1e-4)
    assert bs_price(**STD, opt_type="CE") == pytest.approx(10.4506, abs=1e-4)
    assert bs_price(**STD, opt_type="PE") == pytest.approx(5.5735, abs=1e-4)


def test_known_greeks_standard_case():
    c = bs_greeks(**STD, opt_type="CE")
    p = bs_greeks(**STD, opt_type="PE")
    assert c.delta == pytest.approx(0.6368, abs=1e-4)
    assert p.delta == pytest.approx(-0.3632, abs=1e-4)
    assert c.gamma == pytest.approx(0.018762, abs=1e-6) and p.gamma == pytest.approx(c.gamma)
    assert c.vega == pytest.approx(0.37524, abs=1e-5) and p.vega == pytest.approx(c.vega)  # per vol point
    assert c.theta == pytest.approx(-6.4140 / 365, abs=1e-5)  # per day
    assert p.theta == pytest.approx(-1.6579 / 365, abs=1e-5)


def test_black76_futures_known_value():
    # F=100,K=100,T=1,r=5%,sigma=20% -> both legs 7.5771; delta = e^-rT N(d1)
    assert bs_price(100, 100, 1, 0.05, 0.2, "CE", b=0.0) == pytest.approx(7.5771, abs=1e-4)
    assert bs_price(100, 100, 1, 0.05, 0.2, "PE", b=0.0) == pytest.approx(7.5771, abs=1e-4)
    assert bs_greeks(100, 100, 1, 0.05, 0.2, "CE", b=0.0).delta == pytest.approx(0.5135, abs=1e-4)


@pytest.mark.parametrize("b", [None, 0.0])
@pytest.mark.parametrize("K", [80, 100, 120])
def test_put_call_parity(K, b):
    r, T, S = 0.05, 0.75, 100
    bb = r if b is None else b
    c = bs_price(S, K, T, r, 0.3, "CE", b)
    p = bs_price(S, K, T, r, 0.3, "PE", b)
    import math
    assert c - p == pytest.approx(S * math.exp((bb - r) * T) - K * math.exp(-r * T), abs=1e-9)


@pytest.mark.parametrize("opt", ["CE", "PE"])
@pytest.mark.parametrize("b", [None, 0.0])
def test_greeks_match_finite_differences(opt, b):
    """Independent check: analytic Greeks must equal numerical derivatives of the price."""
    S, K, T, r, s = 24500.0, 24600.0, 0.05, 0.065, 0.14
    price = lambda S=S, T=T, s=s: bs_price(S, K, T, r, s, opt, b)
    g = bs_greeks(S, K, T, r, s, opt, b)
    h = 0.5
    assert g.delta == pytest.approx((price(S=S + h) - price(S=S - h)) / (2 * h), rel=1e-6)
    assert g.gamma == pytest.approx((price(S=S + h) - 2 * price() + price(S=S - h)) / h**2, rel=1e-4)
    hv = 1e-5
    assert g.vega == pytest.approx((price(s=s + hv) - price(s=s - hv)) / (2 * hv) / 100, rel=1e-6)
    hd = 1e-6  # theta = -dPrice/dT, per calendar day
    assert g.theta == pytest.approx(-(price(T=T + hd) - price(T=T - hd)) / (2 * hd) / 365, rel=1e-5)


def test_delta_signs_and_bounds():
    for K in (23000, 24500, 26000):
        c, p = bs_greeks(24500, K, 0.02, 0.065, 0.15, "CE"), bs_greeks(24500, K, 0.02, 0.065, 0.15, "PE")
        assert 0 <= c.delta <= 1 and -1 <= p.delta <= 0
        assert c.gamma > 0 and c.vega > 0 and c.theta < 0  # long options decay


@pytest.mark.parametrize("opt", ["CE", "PE"])
@pytest.mark.parametrize("b", [None, 0.0])
@pytest.mark.parametrize("sigma", [0.05, 0.12, 0.30, 1.2])
@pytest.mark.parametrize("K", [23500, 24500, 25500])
def test_iv_roundtrip(opt, b, sigma, K):
    S, T, r = 24500.0, 7 / 365, 0.065
    px = bs_price(S, K, T, r, sigma, opt, b)
    iv = implied_vol(px, S, K, T, r, opt, b)
    floor = bs_price(S, K, T, r, 1e-4, opt, b)
    if px - floor < 0.05:  # < 1 tick of time value: IV is not identifiable, must say so
        assert iv is None
    else:
        assert iv == pytest.approx(sigma, abs=2e-4)


def test_iv_roundtrip_actually_exercises_most_cases():
    """Guard against the roundtrip test passing vacuously via the None branch."""
    S, T, r = 24500.0, 7 / 365, 0.065
    solved = sum(
        implied_vol(bs_price(S, K, T, r, sg, o, b), S, K, T, r, o, b) is not None
        for o in ("CE", "PE") for b in (None, 0.0) for sg in (0.05, 0.12, 0.30, 1.2) for K in (23500, 24500, 25500)
    )
    assert solved >= 30  # of 48


def test_iv_returns_none_for_impossible_prices():
    S, K, T, r = 24500.0, 24500.0, 5 / 365, 0.065
    assert implied_vol(0.0, S, K, T, r, "CE") is None  # zero/stale quote
    assert implied_vol(-3.0, S, K, T, r, "CE") is None
    assert implied_vol(S + 100, S, K, T, r, "CE") is None  # call worth more than the underlying
    assert implied_vol(5.0, S, 23000, T, r, "CE", b=0.0) is None  # below intrinsic: arbitrage
    assert implied_vol(100.0, S, K, 0.0, r, "CE") is None  # expired


def test_expiry_handling_intrinsic_and_step_delta():
    assert bs_price(24600, 24500, 0.0, 0.065, 0.2, "CE") == 100
    assert bs_price(24400, 24500, 0.0, 0.065, 0.2, "CE") == 0
    g = bs_greeks(24400, 24500, 0.0, 0.065, 0.2, "PE")
    assert (g.price, g.delta, g.gamma, g.theta, g.vega) == (100, -1.0, 0, 0, 0)
    assert bs_greeks(24600, 24500, 0.0, 0.065, 0.2, "PE").delta == 0


def test_very_short_dated_stays_finite():
    T = 20 / (365 * 24 * 60)  # 20 minutes
    g = bs_greeks(24500, 24500, T, 0.065, 0.15, "CE")
    assert 0 < g.price < 30 and 0.4 < g.delta < 0.6 and g.gamma > 0
    iv = implied_vol(g.price, 24500, 24500, T, 0.065, "CE")
    assert iv == pytest.approx(0.15, abs=1e-3)


@pytest.mark.parametrize("args,kw", [
    ((0, 100, 1, .05, .2, "CE"), {}), ((100, -1, 1, .05, .2, "CE"), {}),
    ((100, 100, -1, .05, .2, "CE"), {}), ((100, 100, 1, .05, 0, "CE"), {}),
    ((100, 100, 1, .05, .2, "XX"), {}),
])
def test_invalid_inputs_raise(args, kw):
    with pytest.raises(GreeksError):
        bs_price(*args, **kw)


def test_time_to_expiry_uses_ist_close():
    now = datetime(2026, 11, 9, 9, 15, tzinfo=IST)
    t = time_to_expiry(now, date(2026, 11, 9))
    assert t == pytest.approx(6.25 * 3600 / (365 * 24 * 3600))  # 09:15 -> 15:30
    assert time_to_expiry(datetime(2026, 11, 9, 16, 0, tzinfo=IST), date(2026, 11, 9)) == 0
    assert time_to_expiry(datetime(2026, 11, 2, 15, 30, tzinfo=IST), date(2026, 11, 9)) == pytest.approx(7 / 365)
    with pytest.raises(GreeksError):
        time_to_expiry(datetime(2026, 11, 9, 9, 15), date(2026, 11, 9))  # naive datetime refused


def test_analyse_end_to_end_with_futures_underlying():
    F, K, T = 24530.0, 24500.0, 3 / 365
    px = bs_price(F, K, T, 0.065, 0.13, "PE", b=0.0)
    a = analyse(px, F, K, T, "PE")
    assert a.iv == pytest.approx(0.13, abs=1e-4)
    assert -1 < a.greeks.delta < 0 and a.greeks.price == pytest.approx(px, abs=1e-4)
    assert analyse(0.0, F, K, T, "PE").iv is None and analyse(0.0, F, K, T, "PE").greeks is None


def test_intrinsic():
    assert intrinsic(100, 90, "CE") == 10 and intrinsic(100, 90, "PE") == 0
