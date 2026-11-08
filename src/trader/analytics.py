"""Weekly performance summary: plain functions over plain data (no database).

Conventions, stated so the numbers can be checked by hand:
* A trade is a WIN if its net P&L (after fees) is > 0, a LOSS if < 0, otherwise breakeven.
* Win rate = wins / all closed trades (breakevens count as trades, not wins).
* Average win / average loss are magnitudes of the winners / losers only.
* Max drawdown = the largest fall of cumulative net P&L from a previous peak, measured
  trade by trade in closing order, starting from 0. It does NOT see losses that
  happened and recovered inside a single open trade.
* Nothing here says a strategy is, or will be, profitable. Small samples are mostly luck.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

MIN_MEANINGFUL_TRADES = 30


@dataclass(frozen=True)
class TradeStat:
    trade_id: int
    closed_at: str  # ISO, IST
    setup: str
    strategy: str
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    followed_rules: bool | None  # None = not reviewed yet


@dataclass(frozen=True)
class SetupRow:
    setup: str
    trades: int
    wins: int
    net_pnl: float

    @property
    def avg_pnl(self) -> float:
        return self.net_pnl / self.trades if self.trades else 0.0


@dataclass
class Summary:
    start: date
    end: date
    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None  # positive magnitude
    payoff_ratio: float | None = None  # avg win / avg loss
    profit_factor: float | None = None  # total wins / total losses
    expectancy: float | None = None  # average net P&L per trade
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage_cost: float = 0.0  # already inside gross P&L; shown so you can see what execution cost
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    best_trade: float | None = None
    worst_trade: float | None = None
    by_setup: list[SetupRow] = field(default_factory=list)
    by_day: list[tuple[str, float]] = field(default_factory=list)
    reviewed: int = 0
    followed_yes: int = 0
    followed_no: int = 0
    net_followed: float = 0.0
    net_not_followed: float = 0.0
    signals_total: int = 0
    signals_approved: int = 0
    rejections_by_code: dict[str, int] = field(default_factory=dict)


def max_drawdown(pnls: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the running total, starting from 0 (returned as a positive number)."""
    peak = total = worst = 0.0
    for p in pnls:
        total += p
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def compute_summary(trades: Sequence[TradeStat], start: date, end: date,
                    signals_total: int = 0, signals_approved: int = 0,
                    rejections_by_code: dict[str, int] | None = None) -> Summary:
    s = Summary(start, end, signals_total=signals_total, signals_approved=signals_approved,
                rejections_by_code=dict(sorted((rejections_by_code or {}).items(), key=lambda kv: -kv[1])))
    ordered = sorted(trades, key=lambda t: (t.closed_at, t.trade_id))
    s.trades = len(ordered)
    if not ordered:
        return s

    pnls = [t.net_pnl for t in ordered]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    s.wins, s.losses, s.breakeven = len(wins), len(losses), len(pnls) - len(wins) - len(losses)
    s.win_rate = s.wins / s.trades
    s.avg_win = sum(wins) / len(wins) if wins else None
    s.avg_loss = -sum(losses) / len(losses) if losses else None
    if s.avg_win is not None and s.avg_loss:
        s.payoff_ratio = s.avg_win / s.avg_loss
    if losses:
        s.profit_factor = sum(wins) / -sum(losses)
    s.expectancy = sum(pnls) / s.trades
    s.gross_pnl = sum(t.gross_pnl for t in ordered)
    s.fees = sum(t.fees for t in ordered)
    s.slippage_cost = sum(t.slippage_cost for t in ordered)
    s.net_pnl = sum(pnls)
    s.max_drawdown = max_drawdown(pnls)
    s.best_trade, s.worst_trade = max(pnls), min(pnls)

    groups: dict[str, list[float]] = defaultdict(list)
    days: dict[str, float] = defaultdict(float)
    for t in ordered:
        groups[t.setup].append(t.net_pnl)
        days[t.closed_at[:10]] += t.net_pnl
    s.by_setup = sorted((SetupRow(k, len(v), sum(1 for x in v if x > 0), sum(v)) for k, v in groups.items()),
                        key=lambda r: -r.net_pnl)
    s.by_day = sorted(days.items())

    for t in ordered:
        if t.followed_rules is None:
            continue
        s.reviewed += 1
        if t.followed_rules:
            s.followed_yes += 1
            s.net_followed += t.net_pnl
        else:
            s.followed_no += 1
            s.net_not_followed += t.net_pnl
    return s


def _rs(x: float | None, sign: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x:+,.0f}" if sign else f"{x:,.0f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def format_summary(s: Summary, title: str = "PERFORMANCE SUMMARY",
                   note: str = "(PAPER results unless you went live)") -> str:
    L: list[str] = []
    L.append(f"{title}  {s.start:%d-%b-%Y} to {s.end:%d-%b-%Y}   {note}")
    L.append("=" * 78)
    L.append(f"Signals seen by the risk engine: {s.signals_total}  (approved {s.signals_approved}, "
             f"refused {s.signals_total - s.signals_approved})")
    if s.rejections_by_code:
        top = ", ".join(f"{c} x{n}" for c, n in list(s.rejections_by_code.items())[:6])
        L.append(f"  Refusals by reason: {top}")
    L.append("")
    if s.trades == 0:
        L.append("No closed trades in this period.")
    else:
        L.append(f"Closed trades : {s.trades}   wins {s.wins}  losses {s.losses}  breakeven {s.breakeven}")
        L.append(f"Win rate      : {_pct(s.win_rate)}")
        L.append(f"Average win   : Rs {_rs(s.avg_win)}        Average loss: Rs {_rs(s.avg_loss)}"
                 f"        Payoff ratio: {_num(s.payoff_ratio)}")
        L.append(f"Profit factor : {_num(s.profit_factor)}      Expectancy per trade: Rs {_rs(s.expectancy, True)}")
        L.append(f"Best / worst  : Rs {_rs(s.best_trade, True)} / Rs {_rs(s.worst_trade, True)}")
        L.append(f"Max drawdown  : Rs {_rs(s.max_drawdown)}   (closed-trade basis)")
        L.append("")
        L.append(f"Gross P&L     : Rs {_rs(s.gross_pnl, True)}")
        L.append(f"Fees          : Rs {_rs(-s.fees, True)}")
        L.append(f"NET P&L       : Rs {_rs(s.net_pnl, True)}")
        L.append(f"(Execution slippage already inside gross P&L: Rs {_rs(s.slippage_cost)} paid versus the quoted price)")
        L.append("")
        L.append("P&L by setup")
        L.append(f"  {'setup':<44}{'trades':>7}{'wins':>6}{'net Rs':>10}{'avg Rs':>9}")
        for r in s.by_setup:
            L.append(f"  {r.setup[:43]:<44}{r.trades:>7}{r.wins:>6}{_rs(r.net_pnl, True):>10}{_rs(r.avg_pnl, True):>9}")
        L.append("")
        L.append("P&L by day")
        for d, p in s.by_day:
            L.append(f"  {d}  Rs {_rs(p, True):>8}")
        L.append("")
        L.append(f"Evening review: {s.reviewed} of {s.trades} trades reviewed "
                 f"(followed the rules: {s.followed_yes}, did not: {s.followed_no})")
        if s.reviewed:
            L.append(f"  Net P&L when rules were followed: Rs {_rs(s.net_followed, True)}"
                     f"   when not: Rs {_rs(s.net_not_followed, True)}")
        if s.reviewed < s.trades:
            L.append(f"  {s.trades - s.reviewed} trade(s) still need review:  python -m trader.journal review")
        L.append("")
        if s.trades < MIN_MEANINGFUL_TRADES:
            L.append(f"CAUTION: only {s.trades} trade{'s' if s.trades != 1 else ''} (under {MIN_MEANINGFUL_TRADES}). At this size the numbers above are")
            L.append("mostly luck, not evidence of skill or of a working strategy.")
    L.append("Reminder: markets are risky and most retail F&O traders lose money. Past results, even good")
    L.append("ones, do not predict future results.")
    return "\n".join(L)
