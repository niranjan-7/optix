import pytest

from tests.exec_helpers import World, leg
from trader.brokers import UnapprovedOrderError
from trader.portfolio import Portfolio
from trader.risk import Code

ALL = ["LONG_OPTION", "VERTICAL_DEBIT", "VERTICAL_CREDIT", "IRON_CONDOR"]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def open_long(w, key="e1"):
    d = w.mgr.submit(w.entry(key))
    assert d.approved, d.summary()
    w.advance(1)
    assert len(w.portfolio.open_positions()) == 1
    return d


# ===================================================================== entries

def test_entry_fills_with_slippage_and_books_position_and_fee(w):
    d = w.mgr.submit(w.entry())
    assert d.approved and w.placed() == [("NIFTY25000CE", "BUY", 1)]
    assert w.portfolio.open_positions() == []            # nothing filled yet (0.5s latency)
    w.advance(1)
    (pos,) = w.portfolio.open_positions()
    assert pos.position_id == "P1" and pos.legs[0].lots == 1
    assert pos.entry_price == pytest.approx(100.30)      # ask 100.25 + 1 tick
    assert (pos.stop_loss, pos.target) == (75.2, 140.0)
    assert w.portfolio.realised_pnl_today() == pytest.approx(-20.0)   # brokerage on entry
    assert w.portfolio.entries_today("ex", w.now().date()) == 1
    done = w.of("execution_done")[0]
    assert done["complete"] and done["legs"][0]["slippage_per_unit"] == pytest.approx(0.05)
    assert w.mgr.active_count == 0
    assert w.risk.status()["open_positions"] == 1


def test_risk_pending_entry_is_released_when_execution_finishes(w, tmp_path):
    w.mgr.submit(w.entry())
    import json
    state = lambda: json.load(open(w.cfg.state_file))["pending_entries"]
    assert state() != {}
    w.advance(1)
    assert state() == {}


def test_rejected_signal_places_no_orders(w):
    d = w.mgr.submit(w.entry(sl=None))
    assert not d.approved and Code.NO_STOP_LOSS in d.codes
    assert w.broker.open_orders() == [] and w.of("order_placed") == [] and w.mgr.active_count == 0


def test_broker_rejection_leaves_no_position_and_alerts(tmp_path):
    w = World(tmp_path, paper={"reject_probability": 1.0, "seed": 1})
    w.mgr.submit(w.entry())
    w.advance(1)
    assert w.portfolio.open_positions() == []
    assert w.of("execution_done")[0]["complete"] is False
    assert any("NOT filled" in e["message"] for e in w.of("warning"))
    assert w.risk.status()["open_positions"] == 0


def test_unfillable_order_is_cancelled_after_timeout(tmp_path):
    w = World(tmp_path, orders={"order_timeout_seconds": 10})
    w.mgr.submit(w.entry())
    w.px["25000CE"] = (110.0, 0.5)                       # market runs away beyond our limit
    w.advance(9.5)
    assert w.mgr.active_count == 1 and w.portfolio.open_positions() == []
    w.advance(3)
    assert w.mgr.active_count == 0 and w.portfolio.open_positions() == []
    assert w.of("order_cancel_sent") and any("NOT filled" in e["message"] for e in w.of("warning"))


def test_partial_fills_are_booked_incrementally(tmp_path):
    w = World(tmp_path, paper={"partial_fill_probability": 1.0, "partial_fill_fraction": 0.5},
              risk={"limits": {"max_lots_per_order": 2}}, px={"25000CE": (60.0, 0.5)})
    w.mgr.submit(w.entry(legs=[leg(lots=2)], sl=50.0, target=90.0))
    w.advance(0.5)
    assert w.portfolio.open_positions()[0].legs[0].lots == 1     # first half visible immediately
    w.advance(1)
    (pos,) = w.portfolio.open_positions()
    assert pos.legs[0].lots == 2 and pos.entry_price == pytest.approx(60.30)
    assert [f["lots"] for f in w.of("fill")] == [1, 1]
    # brokerage is per ORDER, not per partial fill
    assert w.portfolio.realised_pnl_today() == pytest.approx(-20.0)


def test_partial_then_timeout_keeps_the_filled_part(tmp_path):
    w = World(tmp_path, paper={"partial_fill_probability": 1.0}, orders={"order_timeout_seconds": 5},
              risk={"limits": {"max_lots_per_order": 2}}, px={"25000CE": (60.0, 0.5)})
    w.mgr.submit(w.entry(legs=[leg(lots=2)], sl=50.0, target=90.0))
    w.advance(0.5)                                       # 1 lot filled
    w.px["25000CE"] = (75.0, 0.5)                        # remainder can no longer fill within the limit
    w.advance(8)
    (pos,) = w.portfolio.open_positions()
    assert pos.legs[0].lots == 1                         # what filled is tracked, not forgotten
    done = w.of("execution_done")[0]
    assert done["complete"] is False and done["legs"][0]["filled_lots"] == 1


# ======================================================================= exits

def test_exit_realises_pnl_and_closes_position(w):
    open_long(w)
    d = w.mgr.submit(w.exit())
    assert d.approved
    w.advance(1)
    assert w.portfolio.open_positions() == []
    # exit sells at bid 99.75 - 1 tick = 99.70 ; entry 100.30 ; 65 units
    expected = -20 + (99.70 - 100.30) * 65 - 20
    assert w.portfolio.realised_pnl_today() == pytest.approx(expected)
    closed = w.of("position_closed")[0]
    assert closed["realised_pnl"] == pytest.approx(expected) and closed["fees"] == pytest.approx(40)
    assert w.risk.status()["open_positions"] == 0


def test_second_exit_while_first_is_active_is_rejected(w):
    open_long(w)
    assert w.mgr.submit(w.exit("x1")).approved
    d = w.mgr.submit(w.exit("x2"))
    assert Code.EXIT_IN_FLIGHT in d.codes
    assert len(w.of("order_placed")) == 2                # entry + ONE exit order


def test_exit_that_does_not_fill_can_be_retried_after_timeout(tmp_path):
    w = World(tmp_path, orders={"order_timeout_seconds": 5})
    open_long(w)
    w.px.pop("25000CE")                                  # no quote => cannot price/fill
    assert w.mgr.submit(w.exit("x1")).approved
    w.advance(1)
    assert len(w.portfolio.open_positions()) == 1
    w.px["25000CE"] = (100.0, 0.5)
    assert w.mgr.submit(w.exit("x2")).approved           # the first attempt was released, so a retry is allowed
    w.advance(1)
    assert w.portfolio.open_positions() == []


# ================================================================== bypass guard

def test_broker_refuses_anything_not_approved_by_risk(w):
    d = w.mgr.submit(w.entry())
    order, other = d.order, w.entry("other")
    good_leg = order.signal.legs[0]
    with pytest.raises(UnapprovedOrderError):
        w.broker.place_order(other, good_leg, 1, 101.0)                  # a bare Signal, not an ApprovedOrder
    with pytest.raises(UnapprovedOrderError):
        w.broker.place_order(order, leg("BUY", 25100), 1, 61.0)         # leg not in the approved signal
    with pytest.raises(ValueError):
        w.broker.place_order(order, good_leg, 2, 101.0)                  # more lots than approved
    with pytest.raises(ValueError):
        w.broker.place_order(order, good_leg, 1, 0.0)
    with pytest.raises(ValueError):
        w.broker.place_order(order, good_leg, 1, float("nan"))


# ===================================================================== kill switch

def test_kill_closes_open_positions_through_the_risk_engine(w):
    open_long(w)
    w.risk.kill("test")
    w.advance(2)
    assert w.portfolio.open_positions() == []
    assert w.risk.status()["kill_active"]
    assert Code.KILL_SWITCH in w.mgr.submit(w.entry("again")).codes     # still blocked afterwards
    assert any(e["type"] == "cancel_all" for e in w.events)


def test_kill_cancels_an_entry_that_is_still_in_flight(w):
    w.mgr.submit(w.entry())                                             # placed, not yet filled
    w.risk.kill("test")
    w.advance(2)
    assert w.portfolio.open_positions() == [] and w.mgr.active_count == 0
    assert w.broker.open_orders() == []


def test_kill_mid_partial_fill_closes_what_was_bought_and_cancels_the_rest(tmp_path):
    w = World(tmp_path, paper={"partial_fill_probability": 1.0}, risk={"limits": {"max_lots_per_order": 2}},
              px={"25000CE": (60.0, 0.5)})
    w.mgr.submit(w.entry(legs=[leg(lots=2)], sl=50.0, target=90.0))
    w.advance(0.5)
    assert w.portfolio.open_positions()[0].legs[0].lots == 1
    w.risk.kill("test")
    w.advance(3)
    assert w.portfolio.open_positions() == [] and w.mgr.active_count == 0


def test_daily_loss_halt_squares_off(w):
    open_long(w)
    w.px["25000CE"] = (60.0, 0.5)                                       # (59.75-100.30)*65 ~ -2,600
    w.risk.poll(); assert w.portfolio.open_positions()                  # not yet over the limit...
    w.px["25000CE"] = (30.0, 0.5)                                       # ~ -4,500 : over the Rs4,000 limit
    w.risk.poll()
    w.advance(2)
    assert w.portfolio.open_positions() == []
    assert w.risk.status()["day_halted"]


def test_forced_end_of_day_square_off(w):
    open_long(w)
    w.t += (15 * 3600 + 20 * 60) - (10 * 3600 + 30 * 60)                # jump to 15:20 IST
    w.risk.poll()
    w.advance(2)
    assert w.portfolio.open_positions() == []


def test_straggler_is_retried_while_positions_must_be_flat(tmp_path):
    w = World(tmp_path, orders={"order_timeout_seconds": 3, "flatten_retry_seconds": 4})
    open_long(w)
    w.px.pop("25000CE")                                                 # market data vanishes
    w.risk.kill("test")
    w.advance(10)
    assert len(w.portfolio.open_positions()) == 1                       # could not close yet; still trying
    assert any("no market quote" in e["message"] for e in w.of("warning"))
    w.px["25000CE"] = (100.0, 0.5)
    w.advance(10)
    assert w.portfolio.open_positions() == []


def test_emergency_exit_accepts_a_wider_price_than_normal(w):
    open_long(w)
    w.risk.kill("test")
    sell = [e for e in w.of("order_placed") if e["side"] == "SELL"][0]
    assert sell["limit_price"] == pytest.approx(94.75)                  # bid 99.75 less 5%, tick-rounded down
    w2 = World(w.cfg and __import__("pathlib").Path(str(w.cfg.state_file)).parent / "b")
    w2.mgr.submit(w2.entry()); w2.advance(1)
    w2.mgr.submit(w2.exit())
    sell2 = [e for e in w2.of("order_placed") if e["side"] == "SELL"][0]
    assert sell2["limit_price"] == pytest.approx(98.75)                 # normal: 1%


# ===================================================================== spreads

def credit_spread(w, lots=1, key="cs"):
    return w.entry(key, legs=[leg("SELL", 25000, lots=lots), leg("BUY", 25100, lots=lots)], sl=80.0, target=10.0)


def test_protective_wing_is_bought_before_the_short_is_sold(tmp_path):
    w = World(tmp_path, structures=ALL, px={"25000CE": (110.0, 0.5), "25100CE": (60.0, 0.5)})
    assert w.mgr.submit(credit_spread(w)).approved
    assert w.placed() == [("NIFTY25100CE", "BUY", 1)]                   # ONLY the long wing so far
    w.advance(1)
    assert ("NIFTY25000CE", "SELL", 1) in w.placed() and w.placed()[0][1] == "BUY"
    w.advance(1)
    (pos,) = w.portfolio.open_positions()
    assert {(l.strike, l.side) for l in pos.legs} == {(25000, "SELL"), (25100, "BUY")}


def test_short_is_never_sold_if_the_protective_buy_fails(tmp_path):
    w = World(tmp_path, structures=ALL, paper={"reject_probability": 1.0, "seed": 3},
              px={"25000CE": (110.0, 0.5), "25100CE": (60.0, 0.5)})
    assert w.mgr.submit(credit_spread(w)).approved
    w.advance(3)
    assert w.placed() == [("NIFTY25100CE", "BUY", 1)]                  # the short was never even attempted
    assert w.of("leg_skipped") and "uncovered" in w.of("leg_skipped")[0]["reason"]
    assert w.portfolio.open_positions() == []


def test_short_size_follows_the_protection_that_actually_filled(tmp_path):
    big = {"max_lots_per_order": 2, "max_loss_per_trade": 5000, "max_loss_per_day": 8000, "max_structure_loss": 20000}
    w = World(tmp_path, structures=ALL, risk={"limits": big},
              paper={"partial_fill_probability": 1.0}, orders={"order_timeout_seconds": 3},
              px={"25000CE": (110.0, 0.5), "25100CE": (60.0, 0.5)})
    assert w.mgr.submit(credit_spread(w, lots=2)).approved
    w.advance(0.5)                                                      # 1 of 2 protective lots filled
    w.px["25100CE"] = (75.0, 0.5)                                       # remainder runs away, then times out
    w.advance(5)
    shorts = [e for e in w.of("order_placed") if e["side"] == "SELL"]
    assert [e["lots"] for e in shorts] == [1]                           # sold 1 lot of the short, not 2
    (pos,) = w.portfolio.open_positions()
    held = {(l.side, l.lots) for l in pos.legs}
    assert held == {("BUY", 1), ("SELL", 1)}


def test_exit_buys_back_the_short_before_selling_the_long(tmp_path):
    w = World(tmp_path, structures=ALL, px={"25000CE": (110.0, 0.5), "25100CE": (60.0, 0.5)})
    w.mgr.submit(credit_spread(w)); w.advance(3)
    n = len(w.of("order_placed"))
    sig = w.exit(legs=[leg("BUY", 25000), leg("SELL", 25100)])
    assert w.mgr.submit(sig).approved
    new = w.placed()[n:]
    assert new == [("NIFTY25000CE", "BUY", 1)]                          # buy-back first
    w.advance(1)
    assert w.placed()[n:][1] == ("NIFTY25100CE", "SELL", 1)             # only then the long wing
    w.advance(1)
    assert w.portfolio.open_positions() == []


def test_long_wing_is_kept_if_the_short_cannot_be_bought_back(tmp_path):
    w = World(tmp_path, structures=ALL, px={"25000CE": (110.0, 0.5), "25100CE": (60.0, 0.5)},
              orders={"order_timeout_seconds": 3})
    w.mgr.submit(credit_spread(w)); w.advance(3)
    assert w.mgr.submit(w.exit(legs=[leg("BUY", 25000), leg("SELL", 25100)])).approved
    w.px["25000CE"] = (130.0, 0.5)                                      # short leg runs away AFTER we priced the buy-back
    w.advance(6)
    (pos,) = w.portfolio.open_positions()
    assert {(l.strike, l.side) for l in pos.legs} == {(25000, "SELL"), (25100, "BUY")}   # still hedged
    sells = [e for e in w.of("order_placed") if e["side"] == "SELL" and e["symbol"] == "NIFTY25100CE"]
    assert sells == []                                                  # the long wing was never sold: the short still needs it


# ===================================================================== start-up

def test_recover_cancels_stale_orders_and_reports_mismatches(w):
    w.mgr.submit(w.entry())
    assert len(w.broker.open_orders()) == 1
    assert w.mgr.recover() == []                                        # paper broker can't report positions
    assert w.broker.open_orders() == []

    class Reporting(type(w.broker)):
        def net_positions(self): return {"25000CE": 65, "9999": -65}
    w.mgr._broker = Reporting(w.quote, w.econf.paper, clock=w.epoch)
    issues = w.mgr.reconcile()
    assert len(issues) == 2 and any("9999" in i for i in issues)
    assert any("POSITION MISMATCH" in e["message"] for e in w.of("warning"))


def test_positions_survive_a_restart(w, tmp_path):
    open_long(w)
    p2 = Portfolio(w.quote, state_file=tmp_path / "portfolio.json", clock=w.now)
    (pos,) = p2.open_positions()
    assert pos.position_id == "P1" and pos.legs[0].token == "25000CE" and pos.entry_price == pytest.approx(100.30)
    assert p2.realised_pnl_today() == pytest.approx(-20.0) and p2.entries_today("ex", w.now().date()) == 1


def test_kill_also_cancels_orphan_broker_orders_no_execution_is_tracking(w):
    """E.g. an order left working after a crash: only the broker-level cancel-all can catch it."""
    d = w.risk.evaluate(w.entry("orphan"))
    w.broker.place_order(d.order, d.order.signal.legs[0], 1, 101.0)      # placed outside any ExecutionManager flow
    assert len(w.broker.open_orders()) == 1 and w.mgr.active_count == 0
    w.risk.kill("test")
    assert w.broker.open_orders() == []
