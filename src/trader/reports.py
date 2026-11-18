"""Monthly P&L report and safe CSV helpers: plain functions over plain data (no database).

Conventions (same as analytics.py, so numbers agree with the weekly summary):
* A month is the calendar month (IST) in which a trade CLOSED.
* A trade is a WIN if its net P&L (after fees) is > 0, a LOSS if < 0, otherwise breakeven.
* "Max drawdown" inside a month is measured trade by trade from that month's own starting point (0).
* "Cumulative net" carries the running total across months, so it shows the whole story.
* Nothing here says a strategy is, or will be, profitable. Small samples are mostly luck.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Sequence

from .analytics import MIN_MEANINGFUL_TRADES, TradeStat, max_drawdown


@dataclass(frozen=True)
class MonthRow:
    month: str  # "2026-11"
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    gross_pnl: float
    fees: float
    net_pnl: float
    best_trade: float
    worst_trade: float
    max_drawdown: float
    cumulative_net: float


def monthly_report(trades: Sequence[TradeStat]) -> list[MonthRow]:
    """One row per calendar month that had at least one closed trade, oldest first."""
    groups: dict[str, list[TradeStat]] = defaultdict(list)
    for t in sorted(trades, key=lambda t: (t.closed_at, t.trade_id)):
        groups[t.closed_at[:7]].append(t)
    rows: list[MonthRow] = []
    running = 0.0
    for month in sorted(groups):
        ts = groups[month]
        pnls = [t.net_pnl for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        running += sum(pnls)
        rows.append(MonthRow(
            month=month, trades=len(ts), wins=wins, losses=sum(1 for p in pnls if p < 0), win_rate=wins / len(ts),
            gross_pnl=sum(t.gross_pnl for t in ts), fees=sum(t.fees for t in ts), net_pnl=sum(pnls),
            best_trade=max(pnls), worst_trade=min(pnls), max_drawdown=max_drawdown(pnls), cumulative_net=running))
    return rows


def daily_curve(trades: Sequence[TradeStat]) -> list[dict[str, Any]]:
    """Day-by-day net P&L with the running total: the data behind the dashboard's P&L curve."""
    days: dict[str, float] = defaultdict(float)
    for t in trades:
        days[t.closed_at[:10]] += t.net_pnl
    out, running = [], 0.0
    for d in sorted(days):
        running += days[d]
        out.append({"day": d, "net": days[d], "cumulative": running})
    return out


# ------------------------------------------------------------------ CSV safety

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(v: Any) -> Any:
    """Stop spreadsheet "formula injection": a text cell that starts with = + - @ would be run as a formula by
    Excel. Trade notes and reasons are free text (some from outside signals), so text gets a leading apostrophe.
    Real numbers are left alone, so negative P&L stays a number."""
    if isinstance(v, str) and v.startswith(_FORMULA_STARTS):
        return "'" + v
    return v


def rows_to_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([safe_cell(c) for c in r])
    return buf.getvalue()


def monthly_csv(rows: Sequence[MonthRow]) -> str:
    header = list(MonthRow.__dataclass_fields__)
    return rows_to_csv(header, ([asdict(r)[c] for c in header] for r in rows))


# ------------------------------------------------------------------ text report

def _rs(x: float, sign: bool = True) -> str:
    return f"{x:+,.0f}" if sign else f"{x:,.0f}"


def format_monthly(rows: Sequence[MonthRow], note: str = "(PAPER results unless you went live)") -> str:
    L = [f"MONTHLY P&L REPORT   {note}", "=" * 96]
    if not rows:
        L.append("No closed trades in this period.")
    else:
        L.append(f"{'month':<9}{'trades':>7}{'wins':>6}{'win %':>7}{'gross Rs':>11}{'fees Rs':>9}{'NET Rs':>10}"
                 f"{'best Rs':>10}{'worst Rs':>10}{'max DD Rs':>11}{'running Rs':>12}")
        for r in rows:
            L.append(f"{r.month:<9}{r.trades:>7}{r.wins:>6}{r.win_rate * 100:>6.0f}%{_rs(r.gross_pnl):>11}"
                     f"{_rs(-r.fees):>9}{_rs(r.net_pnl):>10}{_rs(r.best_trade):>10}{_rs(r.worst_trade):>10}"
                     f"{_rs(r.max_drawdown, False):>11}{_rs(r.cumulative_net):>12}")
        total_trades = sum(r.trades for r in rows)
        L.append("-" * 96)
        L.append(f"{'TOTAL':<9}{total_trades:>7}{sum(r.wins for r in rows):>6}{'':>7}{_rs(sum(r.gross_pnl for r in rows)):>11}"
                 f"{_rs(-sum(r.fees for r in rows)):>9}{_rs(sum(r.net_pnl for r in rows)):>10}")
        if total_trades < MIN_MEANINGFUL_TRADES:
            L.append("")
            L.append(f"CAUTION: only {total_trades} trade{'s' if total_trades != 1 else ''} (under {MIN_MEANINGFUL_TRADES}). "
                     "At this size the numbers above are mostly luck, not evidence of skill.")
    L.append("Reminder: markets are risky and most retail F&O traders lose money. Past results do not predict future results.")
    return "\n".join(L)
