from datetime import date

import pytest

from trader.analytics import MIN_MEANINGFUL_TRADES, TradeStat, compute_summary, format_summary, max_drawdown


def T(i, day, pnl, setup="s1", followed=None, gross=None, fees=0.0, slip=0.0):
    return TradeStat(i, f"2026-11-{day:02d}T11:00:00+05:30", setup, "strat", pnl + fees if gross is None else gross,
                     fees, slip, pnl, followed)


# Hand-worked example, in closing order:  +500, -200, -300, +100, +400, -600, 0
#   running total : 500, 300, 0, 100, 500, -100, -100     peak 500 -> low -100  => max drawdown 600
#   wins (500,100,400)=1000 over 3 trades ; losses (200,300,600)=1100 over 3 ; 1 breakeven ; 7 trades
SEQ = [500, -200, -300, 100, 400, -600, 0]
TRADES = [T(i + 1, 2 + i // 2, p, setup="A" if p >= 0 else "B", followed=(p > 0) if i < 5 else None)
          for i, p in enumerate(SEQ)]


def test_drawdown_hand_cases():
    assert max_drawdown(SEQ) == 600
    assert max_drawdown([]) == 0
    assert max_drawdown([100, 200, 300]) == 0                 # only goes up
    assert max_drawdown([-100, -50]) == 150                   # starts underwater: peak is the starting 0
    assert max_drawdown([100, -50, 20, -80]) == 110           # peak 100 -> total -10


def test_drawdown_uses_the_worst_dip_not_the_last_one():
    # total: 50, -50, 150, 140 -> dips: 100 (50 -> -50), then 10 (150 -> 140): worst is 100
    assert max_drawdown([50, -100, 200, -10]) == 100


def test_summary_headline_numbers():
    s = compute_summary(TRADES, date(2026, 11, 2), date(2026, 11, 8))
    assert (s.trades, s.wins, s.losses, s.breakeven) == (7, 3, 3, 1)
    assert s.win_rate == pytest.approx(3 / 7)
    assert s.avg_win == pytest.approx(1000 / 3) and s.avg_loss == pytest.approx(1100 / 3)
    assert s.payoff_ratio == pytest.approx(1000 / 1100)
    assert s.profit_factor == pytest.approx(1000 / 1100)
    assert s.expectancy == pytest.approx(-100 / 7)
    assert s.net_pnl == -100 and s.max_drawdown == 600
    assert (s.best_trade, s.worst_trade) == (500, -600)


def test_order_of_input_does_not_matter():
    a = compute_summary(TRADES, date(2026, 11, 2), date(2026, 11, 8))
    b = compute_summary(list(reversed(TRADES)), date(2026, 11, 2), date(2026, 11, 8))
    assert a.max_drawdown == b.max_drawdown == 600 and a.by_day == b.by_day


def test_grouping_by_setup_and_day():
    s = compute_summary(TRADES, date(2026, 11, 2), date(2026, 11, 8))
    rows = {r.setup: r for r in s.by_setup}
    assert (rows["A"].trades, rows["A"].wins, rows["A"].net_pnl) == (4, 3, 1000)        # 500, 100, 400, 0
    assert (rows["B"].trades, rows["B"].wins, rows["B"].net_pnl) == (3, 0, -1100)
    assert rows["A"].avg_pnl == 250
    assert [r.setup for r in s.by_setup] == ["A", "B"]                                  # best first
    # trades 1-2 on the 2nd (+500-200), 3-4 on the 3rd (-300+100), 5-6 on the 4th (+400-600), 7 on the 5th (0)
    assert dict(s.by_day) == {"2026-11-02": 300, "2026-11-03": -200, "2026-11-04": -200, "2026-11-05": 0}


def test_gross_fees_slippage_are_summed_separately():
    t = [T(1, 2, 100, gross=130, fees=30, slip=5), T(2, 3, -50, gross=-20, fees=30, slip=7)]
    s = compute_summary(t, date(2026, 11, 2), date(2026, 11, 3))
    assert (s.gross_pnl, s.fees, s.net_pnl, s.slippage_cost) == (110, 60, 50, 12)


def test_review_adherence_stats():
    s = compute_summary(TRADES, date(2026, 11, 2), date(2026, 11, 8))
    # reviewed = first five: +500(y) -200(n) -300(n) +100(y) +400(y)
    assert (s.reviewed, s.followed_yes, s.followed_no) == (5, 3, 2)
    assert s.net_followed == 1000 and s.net_not_followed == -500


def test_edge_cases_no_wins_no_losses_no_trades():
    only_wins = compute_summary([T(1, 2, 10), T(2, 3, 20)], date(2026, 11, 2), date(2026, 11, 3))
    assert only_wins.avg_loss is None and only_wins.payoff_ratio is None and only_wins.profit_factor is None
    assert only_wins.win_rate == 1 and only_wins.max_drawdown == 0
    only_losses = compute_summary([T(1, 2, -10)], date(2026, 11, 2), date(2026, 11, 3))
    assert only_losses.avg_win is None and only_losses.profit_factor == 0 and only_losses.win_rate == 0
    none = compute_summary([], date(2026, 11, 2), date(2026, 11, 3), 4, 1, {"NO_STOP_LOSS": 2, "DUPLICATE": 1})
    assert none.trades == 0 and none.win_rate is None and none.net_pnl == 0
    assert list(none.rejections_by_code) == ["NO_STOP_LOSS", "DUPLICATE"] and (none.signals_total, none.signals_approved) == (4, 1)


def test_text_report_contains_the_key_facts_and_the_honesty_notes():
    text = format_summary(compute_summary(TRADES, date(2026, 11, 2), date(2026, 11, 8), 12, 7, {"FEED_UNHEALTHY": 5}))
    for needle in ("Win rate      : 43%", "Max drawdown  : Rs 600", "NET P&L       : Rs -100", "FEED_UNHEALTHY x5",
                   "P&L by setup", "2 of 7 trades", "reviewed", "most retail F&O traders lose money", "CAUTION: only 7 trades"):
        assert needle in text or needle.replace("2 of 7", "5 of 7") in text, needle
    assert "5 of 7 trades reviewed" in text and "2 trade(s) still need review" in text
    assert "No closed trades" in format_summary(compute_summary([], date(2026, 11, 2), date(2026, 11, 3)))


def test_no_caution_with_a_large_sample():
    many = [T(i, 2 + i % 5, 10 if i % 2 else -5) for i in range(1, MIN_MEANINGFUL_TRADES + 1)]
    assert "CAUTION" not in format_summary(compute_summary(many, date(2026, 11, 2), date(2026, 11, 8)))
