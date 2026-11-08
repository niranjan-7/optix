"""Black-Scholes price, implied volatility (bisection) and Greeks.

Generalised Black-Scholes with a cost-of-carry term `b`:
  * spot underlying:    b = r - q          (q = dividend yield, ~0 for NIFTY here)
  * futures underlying: b = 0              (this is Black-76)
We prefer the futures price as the underlying (per the project plan): it already
contains the market's view of carry/dividends, so we don't need to guess them.
For European index options (NIFTY) this is the right model; it is NOT valid for
American-style stock options.

Units (chosen to match what a trader reads on a screen):
  * sigma / iv   : annualised, as a fraction (0.15 = 15%)
  * T            : years (calendar days / 365)
  * vega         : price change per 1 volatility *point* (1% = 0.01 in sigma)
  * theta        : price change per calendar *day*
  * delta, gamma : per 1.0 rupee/point move in the underlying
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import erf, exp, log, pi, sqrt

# Indian markets: options expire at 15:30 IST on the expiry date.
IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_TIME = time(15, 30)

# ASSUMPTION: a rough Indian risk-free rate (T-bill/repo region). Greeks for
# short-dated index options are insensitive to it; revisit if it matters.
DEFAULT_RATE = 0.065

# Below this, T is treated as "expired" (about 30 seconds) to avoid dividing by ~0.
MIN_T = 1e-6

# If an option's price exceeds its no-vol floor by less than one NIFTY tick, the
# quote says nothing about volatility (deep ITM ~ intrinsic, or deep OTM ~ 0):
# hundreds of different IVs reproduce it. We return None instead of a made-up number.
MIN_TIME_VALUE = 0.05

IV_LOW, IV_HIGH = 1e-4, 5.0  # search bracket: 0.01% .. 500%
IV_PRICE_TOL = 1e-6
IV_MAX_ITER = 200


class GreeksError(ValueError):
    """Invalid inputs (not 'IV not found' — that returns None)."""


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1 vol point


@dataclass(frozen=True)
class OptionAnalytics:
    """Result for one market quote; iv is None when no volatility fits the price."""
    iv: float | None
    greeks: Greeks | None


def _N(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def _check(S: float, K: float, T: float, sigma: float | None, opt_type: str) -> str:
    ot = opt_type.upper()
    if ot not in ("CE", "PE"):
        raise GreeksError(f"opt_type must be 'CE' or 'PE', got {opt_type!r}")
    if S <= 0 or K <= 0:
        raise GreeksError("Underlying and strike must be positive")
    if T < 0:
        raise GreeksError("T must not be negative")
    if sigma is not None and sigma <= 0:
        raise GreeksError("sigma must be positive")
    return ot


def time_to_expiry(now: datetime, expiry: date) -> float:
    """Years from `now` to 15:30 IST on `expiry` (0 if already past)."""
    if now.tzinfo is None:
        raise GreeksError("`now` must be timezone-aware (use IST) to avoid off-by-hours errors")
    end = datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST)
    return max((end - now).total_seconds(), 0.0) / (365.0 * 24 * 3600)


def intrinsic(S: float, K: float, opt_type: str) -> float:
    return max(S - K, 0.0) if opt_type.upper() == "CE" else max(K - S, 0.0)


def _d1_d2(S: float, K: float, T: float, b: float, sigma: float) -> tuple[float, float]:
    d1 = (log(S / K) + (b + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    return d1, d1 - sigma * sqrt(T)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt_type: str, b: float | None = None) -> float:
    """Theoretical price. `b` is cost of carry (default r = non-dividend spot; 0 = futures)."""
    ot = _check(S, K, T, sigma, opt_type)
    b = r if b is None else b
    if T < MIN_T:
        return intrinsic(S, K, ot)
    d1, d2 = _d1_d2(S, K, T, b, sigma)
    fwd = S * exp((b - r) * T)
    disc_k = K * exp(-r * T)
    if ot == "CE":
        return fwd * _N(d1) - disc_k * _N(d2)
    return disc_k * _N(-d2) - fwd * _N(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str, b: float | None = None) -> Greeks:
    ot = _check(S, K, T, sigma, opt_type)
    b = r if b is None else b
    if T < MIN_T:
        # At expiry: price is intrinsic, delta is a step function, rest vanish.
        itm = (S > K) if ot == "CE" else (S < K)
        delta = (1.0 if itm else 0.0) * (1 if ot == "CE" else -1)
        return Greeks(intrinsic(S, K, ot), delta, 0.0, 0.0, 0.0)

    sq = sqrt(T)
    d1, d2 = _d1_d2(S, K, T, b, sigma)
    carry = exp((b - r) * T)
    disc = exp(-r * T)
    pdf1 = _pdf(d1)

    gamma = carry * pdf1 / (S * sigma * sq)
    vega = S * carry * pdf1 * sq / 100.0
    common = -S * carry * pdf1 * sigma / (2.0 * sq)
    if ot == "CE":
        delta = carry * _N(d1)
        theta_year = common - (b - r) * S * carry * _N(d1) - r * K * disc * _N(d2)
    else:
        delta = -carry * _N(-d1)
        theta_year = common + (b - r) * S * carry * _N(-d1) + r * K * disc * _N(-d2)
    return Greeks(
        price=bs_price(S, K, T, r, sigma, ot, b),
        delta=delta, gamma=gamma, theta=theta_year / 365.0, vega=vega,
    )


def implied_vol(price: float, S: float, K: float, T: float, r: float, opt_type: str, b: float | None = None) -> float | None:
    """Solve for sigma by bisection. Returns None if no sensible IV exists.

    Why bisection: price is strictly increasing in sigma, so bisection always
    converges and cannot overshoot like Newton's method does when vega ~ 0
    (deep OTM / near expiry). Speed is irrelevant at our chain size.
    None (not an exception) because a stale or zero quote is routine, and the
    caller should simply show "n/a" and carry on.
    """
    ot = _check(S, K, T, None, opt_type)
    if price <= 0 or T < MIN_T:
        return None
    # Outside these bounds no volatility can reproduce the price (arbitrage / bad quote).
    lo_price = bs_price(S, K, T, r, IV_LOW, ot, b)
    hi_price = bs_price(S, K, T, r, IV_HIGH, ot, b)
    if price < lo_price - IV_PRICE_TOL or price > hi_price + IV_PRICE_TOL:
        return None
    if price - lo_price < MIN_TIME_VALUE:
        return None

    lo, hi = IV_LOW, IV_HIGH
    for _ in range(IV_MAX_ITER):
        mid = 0.5 * (lo + hi)
        diff = bs_price(S, K, T, r, mid, ot, b) - price
        if abs(diff) < IV_PRICE_TOL or (hi - lo) < 1e-10:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def analyse(price: float, S: float, K: float, T: float, opt_type: str,
            r: float = DEFAULT_RATE, futures_underlying: bool = True) -> OptionAnalytics:
    """One-call helper for the dashboard/strategies: market price in, IV + Greeks out.

    futures_underlying=True means `S` is a futures price (b = 0).
    """
    b = 0.0 if futures_underlying else r
    iv = implied_vol(price, S, K, T, r, opt_type, b)
    if iv is None:
        return OptionAnalytics(None, None)
    return OptionAnalytics(iv, bs_greeks(S, K, T, r, iv, opt_type, b))
