import json
from datetime import datetime, timedelta

import pytest

from tests.test_risk import EXP, TUE, leg
from trader.feed import Tick
from trader.portfolio import FeeModel, Portfolio


class Env:
    def __init__(self, tmp_path=None, fees=None):
        self.t = TUE
        self.q = {}
        self.pf = Portfolio(self.quote, fees or FeeModel(brokerage_per_order=20), (tmp_path / "pf.json") if tmp_path else None, lambda: self.t)

    def quote(self, token):
        if token not in self.q:
            return None
        bid, ask = self.q[token]
        return Tick("NFO", token, (bid + ask) / 2, bid, ask, None, None, None, 0.0)


def test_fee_model():
    f = FeeModel(brokerage_per_order=20, pct_of_turnover=0.1)
    assert f.fee(65, 100, True) == pytest.approx(20 + 6.5)
    assert f.fee(65, 100, False) == pytest.approx(6.5)               # brokerage only on the first fill of an order


def test_entry_averaging_and_single_entry_count():
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", 75.0, 140.0, leg(lots=2), 1, 100.0, True)
    pid2, _ = e.pf.record_entry_fill(pid, "s", 75.0, 140.0, leg(lots=2), 1, 110.0, False)
    assert pid == pid2 == "P1"
    (v,) = e.pf.open_positions()
    assert v.legs[0].lots == 2 and v.entry_price == pytest.approx(105.0)
    assert e.pf.entries_today("s", TUE.date()) == 1                    # one position, however many fills
    assert e.pf.entries_today("s", TUE.date() + timedelta(days=1)) == 0 and e.pf.entries_today("other", TUE.date()) == 0
    assert e.pf.open_positions("s") and e.pf.open_positions("other") == []


def test_long_pnl_partial_and_full_exit_with_fees():
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", 75.0, None, leg(lots=2), 2, 100.0, True)          # fee 20
    pnl, fee, closed = e.pf.record_exit_fill(pid, leg("SELL", lots=2), 1, 110.0, True)            # +10*65, fee 20
    assert (pnl, fee, closed) == (650.0, 20.0, None)
    assert e.pf.open_positions()[0].legs[0].lots == 1
    pnl, fee, closed = e.pf.record_exit_fill(pid, leg("SELL", lots=2), 1, 90.0, True)             # -10*65, fee 20
    assert pnl == -650.0 and closed is not None and e.pf.open_positions() == []
    assert closed.realised_pnl == pytest.approx(-20 + 650 - 20 - 650 - 20)    # lifetime incl. entry fee
    assert closed.fees == pytest.approx(60)
    assert e.pf.realised_pnl_today() == pytest.approx(closed.realised_pnl)
    assert e.pf.closed_trades == [closed]


def test_short_leg_pnl_sign():
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", 80.0, 10.0, leg("SELL", 25000), 1, 110.0, True)
    e.pf.record_entry_fill(pid, "s", 80.0, 10.0, leg("BUY", 25100), 1, 60.0, True)
    pnl, _, _ = e.pf.record_exit_fill(pid, leg("BUY", 25000), 1, 100.0, True)       # sold at 110, bought back at 100
    assert pnl == pytest.approx(10 * 65)
    pnl, _, closed = e.pf.record_exit_fill(pid, leg("SELL", 25100), 1, 50.0, True)  # bought at 60, sold at 50
    assert pnl == pytest.approx(-10 * 65) and closed is not None


def test_exit_that_does_not_match_holdings_is_refused():
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", None, None, leg(), 1, 100.0, True)
    for bad in (dict(closing_leg=leg("SELL", 25100)), dict(closing_leg=leg("BUY"))):      # other contract / wrong side
        with pytest.raises(ValueError):
            e.pf.record_exit_fill(pid, bad["closing_leg"], 1, 100.0, True)
    with pytest.raises(ValueError):
        e.pf.record_exit_fill(pid, leg("SELL", lots=2), 2, 100.0, True)                    # more than held
    with pytest.raises(KeyError):
        e.pf.record_exit_fill("P9", leg("SELL"), 1, 100.0, True)
    with pytest.raises(ValueError):
        e.pf.record_exit_fill(pid, leg("SELL"), 1, 0.0, True)


def test_unrealised_marks_longs_at_bid_and_shorts_at_ask():
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", None, None, leg("BUY", 25100), 1, 60.0, True)
    e.pf.record_entry_fill(pid, "s", None, None, leg("SELL", 25000), 1, 110.0, True)
    e.q = {"25100CE": (62.0, 62.5), "25000CE": (108.0, 108.5)}
    # long: (62.0 - 60) * 65 ; short: (110 - 108.5) * 65
    assert e.pf.unrealised_pnl() == pytest.approx((2.0 + 1.5) * 65)
    e.q.clear()                                                        # data vanishes: keep last marks, don't invent profit/loss
    assert e.pf.unrealised_pnl() == pytest.approx((2.0 + 1.5) * 65)


def test_unrealised_with_no_quote_ever_is_flat_not_an_error():
    e = Env()
    e.pf.record_entry_fill(None, "s", None, None, leg(), 1, 100.0, True)
    assert e.pf.unrealised_pnl() == 0.0


def test_realised_pnl_is_per_day():
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", None, None, leg(), 1, 100.0, True)
    assert e.pf.realised_pnl_today() == -20
    e.t = TUE + timedelta(days=1)
    assert e.pf.realised_pnl_today() == 0.0


def test_net_units_by_token():
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", None, None, leg("BUY", 25100), 1, 60.0, True)
    e.pf.record_entry_fill(pid, "s", None, None, leg("SELL", 25000), 1, 110.0, True)
    assert e.pf.net_units_by_token() == {"25100CE": 65, "25000CE": -65}


def test_persistence_round_trip_including_spreads_and_partial_closes(tmp_path):
    e = Env(tmp_path)
    pid, _ = e.pf.record_entry_fill(None, "s", 80.0, 10.0, leg("SELL", 25000, lots=2), 2, 110.0, True)
    e.pf.record_entry_fill(pid, "s", 80.0, 10.0, leg("BUY", 25100, lots=2), 2, 60.0, True)
    e.pf.record_exit_fill(pid, leg("BUY", 25000, lots=2), 1, 100.0, True)
    p2 = Portfolio(e.quote, FeeModel(brokerage_per_order=20), tmp_path / "pf.json", lambda: e.t)
    a, b = e.pf.open_positions()[0], p2.open_positions()[0]
    assert a == b and {(l.strike, l.side, l.lots) for l in b.legs} == {(25000, "SELL", 1), (25100, "BUY", 2)}
    assert p2.realised_pnl_today() == e.pf.realised_pnl_today() and p2.entries_today("s", TUE.date()) == 1
    nxt, _ = p2.record_entry_fill(None, "s", None, None, leg(), 1, 100.0, True)
    assert nxt == "P2"                                                  # ids keep counting after a restart


def test_corrupt_portfolio_file_stops_startup_instead_of_assuming_flat(tmp_path):
    (tmp_path / "pf.json").write_text("{ nope")
    with pytest.raises(RuntimeError, match="Check your positions at the broker"):
        Env(tmp_path)
    assert list(tmp_path.glob("pf.json.corrupt.*"))
    (tmp_path / "pf2.json").write_text(json.dumps({"counter": 1}))     # valid JSON, wrong shape
    with pytest.raises(RuntimeError):
        Portfolio(lambda t: None, state_file=tmp_path / "pf2.json")
