from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.exec_helpers import World, leg
from trader.brokers import (
    CANCELLED, FILLED, OPEN, PARTIAL, REJECTED, AngelBroker, BrokerError, LiveGate, LiveTradingNotConfirmed,
    PaperBroker, PaperConfig, build_broker, confirm_live_trading,
)
from trader.greeks import IST
from trader.risk import UnapprovedOrderError


def approved(tmp_path, **world_kw):
    w = World(tmp_path, **world_kw)
    d = w.risk.evaluate(w.entry())
    assert d.approved
    return w, d.order


def approved_exit(w):
    """Open a position through the manager, then get the risk engine's approval for closing it."""
    assert w.mgr.submit(w.entry("open-first")).approved
    w.advance(1)
    d = w.risk.evaluate(w.exit("close-it"))
    assert d.approved, d.summary()
    return d.order


# ============================================================== PaperBroker

def test_fill_price_uses_touch_plus_slippage_in_ticks(tmp_path):
    w, o = approved(tmp_path, paper={"slippage_ticks": 2})
    buy = w.broker.place_order(o, o.signal.legs[0], 1, 105.0)
    w.t += 1
    b = w.broker.get_order(buy.order_id)
    assert b.status == FILLED and b.avg_price == pytest.approx(100.35)       # ask 100.25 + 2 ticks
    ex = approved_exit(w)
    sell = w.broker.place_order(ex, ex.signal.legs[0], 1, 90.0)
    w.t += 1
    assert w.broker.get_order(sell.order_id).avg_price == pytest.approx(99.65)   # bid 99.75 - 2 ticks


def test_nothing_fills_before_latency_elapses(tmp_path):
    w, o = approved(tmp_path, paper={"latency_seconds": 2})
    ord_ = w.broker.place_order(o, o.signal.legs[0], 1, 105.0)
    w.t += 1.9
    assert w.broker.get_order(ord_.order_id).status == OPEN
    w.t += 0.2
    assert w.broker.get_order(ord_.order_id).status == FILLED


def test_limit_is_respected_and_order_waits_for_the_market(tmp_path):
    w, o = approved(tmp_path)
    ord_ = w.broker.place_order(o, o.signal.legs[0], 1, 100.0)              # below ask+slippage: not marketable
    w.t += 1
    assert w.broker.get_order(ord_.order_id).status == OPEN
    w.px["25000CE"] = (98.0, 0.5)                                           # market comes to us
    assert w.broker.get_order(ord_.order_id).status == FILLED
    assert w.broker.get_order(ord_.order_id).avg_price == pytest.approx(98.30)


def test_sell_limit_floor_is_respected(tmp_path):
    w = World(tmp_path)
    ex = approved_exit(w)
    s = w.broker.place_order(ex, ex.signal.legs[0], 1, 99.9)                # wants >= 99.90, market bid is 99.75
    w.t += 1
    assert w.broker.get_order(s.order_id).status == OPEN


def test_partial_fill_then_cancel_keeps_the_filled_part(tmp_path):
    w = World(tmp_path, paper={"partial_fill_probability": 1.0}, risk={"limits": {"max_lots_per_order": 2}},
              px={"25000CE": (60.0, 0.5)})
    o2 = w.risk.evaluate(w.entry("big", legs=[leg(lots=2)], sl=50.0, target=90.0)).order
    ord_ = w.broker.place_order(o2, o2.signal.legs[0], 2, 70.0)
    w.t += 0.5
    got = w.broker.get_order(ord_.order_id)
    assert got.status == PARTIAL and got.filled_lots == 1
    w.broker.cancel_order(ord_.order_id)
    got = w.broker.get_order(ord_.order_id)
    assert got.status == CANCELLED and got.filled_lots == 1                 # fill kept
    w.broker.cancel_order(ord_.order_id)                                    # cancelling again is harmless


def test_rejections_are_reproducible_with_a_seed(tmp_path):
    outcomes = []
    for sub in ("a", "b"):
        w, o = approved(tmp_path / sub, paper={"reject_probability": 0.5, "seed": 42})
        ids = [w.broker.place_order(o, o.signal.legs[0], 1, 105.0).order_id for _ in range(8)]
        w.t += 1
        outcomes.append([w.broker.get_order(i).status for i in ids])
    assert outcomes[0] == outcomes[1] and {REJECTED, FILLED} == set(outcomes[0])


def test_callers_get_copies_and_unknown_ids_error(tmp_path):
    w, o = approved(tmp_path)
    ord_ = w.broker.place_order(o, o.signal.legs[0], 1, 105.0)
    ord_.status = "FILLED"                                                   # mutating our copy changes nothing
    assert w.broker.get_order(ord_.order_id).status == OPEN
    with pytest.raises(BrokerError):
        w.broker.get_order("nope")
    with pytest.raises(BrokerError):
        w.broker.cancel_order("nope")
    assert [x.order_id for x in w.broker.open_orders()] == [ord_.order_id]


# ============================================================== live gate

def settings(live=True):
    return SimpleNamespace(live_trading=live, angel_client_id="C1")


PHRASE = "START LIVE C1"


def test_gate_requires_the_env_flag():
    with pytest.raises(LiveTradingNotConfirmed, match="not 'true'"):
        confirm_live_trading(settings(False), lambda _: PHRASE)


def test_gate_refuses_when_nobody_is_at_the_keyboard(monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(LiveTradingNotConfirmed, match="unattended"):
        confirm_live_trading(settings())


@pytest.mark.parametrize("typed", ["", "yes", "start live c1", "START LIVE", "START LIVE C2", PHRASE + "x"])
def test_gate_needs_the_exact_phrase(typed):
    with pytest.raises(LiveTradingNotConfirmed, match="did not match"):
        confirm_live_trading(settings(), lambda _: typed)


def test_gate_is_per_account_and_per_day():
    g = confirm_live_trading(settings(), lambda _: PHRASE)
    assert g.valid_for("C1")
    assert not g.valid_for("C2")
    assert not g.valid_for("C1", now=datetime.now(IST) + timedelta(days=1))          # yesterday's OK does not carry over
    assert not LiveGate("C1", g.day, "0" * 64).valid_for("C1")                          # forged


def test_angel_broker_cannot_be_built_without_a_real_gate():
    for bad in (None, "yes", LiveGate("C1", datetime.now(IST).date().isoformat(), "bad")):
        with pytest.raises(LiveTradingNotConfirmed):
            AngelBroker(SimpleNamespace(), settings(), bad)


def test_build_broker_defaults_to_paper_and_never_goes_live_unconfirmed():
    q = lambda t: None
    assert isinstance(build_broker(settings(False), None, q, PaperConfig()), PaperBroker)
    with pytest.raises(LiveTradingNotConfirmed):
        build_broker(settings(True), None, q, PaperConfig(), input_fn=lambda _: "no")
    b = build_broker(settings(True), SimpleNamespace(), q, PaperConfig(), input_fn=lambda _: PHRASE)
    assert isinstance(b, AngelBroker)


# =============================================================== AngelBroker

class FakeClient:
    def __init__(self):
        self.placed, self.cancelled, self.book, self.positions = [], [], [], []
        self.place_result, self.cancel_resp, self.book_calls = "1001", {"status": True}, 0

    def placeOrder(self, params):
        self.placed.append(params)
        if isinstance(self.place_result, Exception):
            raise self.place_result
        return self.place_result

    def orderBook(self):
        self.book_calls += 1
        return {"status": True, "data": list(self.book)}

    def cancelOrder(self, oid, variety):
        self.cancelled.append((oid, variety))
        return self.cancel_resp

    def position(self):
        return {"status": True, "data": self.positions}


class Rig:
    def __init__(self, tmp_path, live=True, lots=1):
        self.w = World(tmp_path, risk={"limits": {"max_lots_per_order": 2}}, px={"25000CE": (60.0, 0.5)})
        d = self.w.risk.evaluate(self.w.entry("e1", legs=[leg(lots=lots)], sl=50.0, target=90.0))
        assert d.approved, d.summary()
        self.order = d.order
        self.client = FakeClient()
        self.auth = SimpleNamespace(client=self.client, ensure_session=lambda: None)
        self.settings = settings(live)
        self.now = 1000.0
        gate = confirm_live_trading(self.settings, lambda _: PHRASE)
        self.broker = AngelBroker(self.auth, self.settings, gate, clock=lambda: self.now)
        self.leg = self.order.signal.legs[0]

    def place(self, side_leg=None, lots=1, price=101.3):
        return self.broker.place_order(self.order, side_leg or self.leg, lots, price)

    def row(self, **kw):
        base = {"orderid": "1001", "orderstatus": "open", "filledshares": "0", "unfilledshares": "65",
                "averageprice": 0, "text": "", "tradingsymbol": self.leg.symbol, "transactiontype": "BUY",
                "quantity": "65", "price": "101.30"}
        base.update(kw); return base


def test_order_params_sent_to_angel(tmp_path):
    r = Rig(tmp_path)
    o = r.place()
    assert o.order_id == "1001" and o.status == OPEN
    assert r.client.placed == [{
        "variety": "NORMAL", "tradingsymbol": r.leg.symbol, "symboltoken": r.leg.token, "transactiontype": "BUY",
        "exchange": "NFO", "ordertype": "LIMIT", "producttype": "INTRADAY", "duration": "DAY", "price": "101.30",
        "squareoff": "0", "stoploss": "0", "quantity": "65"}]


def test_angel_refuses_unapproved_orders_and_stops_if_live_is_switched_off(tmp_path):
    r = Rig(tmp_path)
    with pytest.raises(UnapprovedOrderError):
        r.broker.place_order(r.order.signal, r.leg, 1, 101.0)
    assert r.client.placed == []
    r.settings.live_trading = False                                          # operator flips the switch off mid-day
    with pytest.raises(LiveTradingNotConfirmed):
        r.place()
    assert r.client.placed == []


@pytest.mark.parametrize("row,status,filled,avg", [
    (dict(orderstatus="complete", filledshares="65", unfilledshares="0", averageprice="100.5"), FILLED, 65, 100.5),
    (dict(orderstatus="rejected", text="RMS: margin exceeded"), REJECTED, 0, 0.0),
    (dict(orderstatus="cancelled"), CANCELLED, 0, 0.0),
    (dict(orderstatus="open"), OPEN, 0, 0.0),
    (dict(orderstatus="validation pending"), OPEN, 0, 0.0),
    (dict(orderstatus="some new status"), OPEN, 0, 0.0),                     # unknown => treated as still open, never as filled
])
def test_order_book_status_mapping(tmp_path, row, status, filled, avg):
    r = Rig(tmp_path)
    r.place()
    r.client.book = [r.row(**row)]
    got = r.broker.get_order("1001")
    assert (got.status, got.filled_qty, got.avg_price) == (status, filled, avg)
    if status == REJECTED:
        assert "margin" in got.message


def test_partly_filled_order_is_reported_as_partial(tmp_path):
    r = Rig(tmp_path, lots=2)
    r.place(lots=2)
    r.client.book = [r.row(orderstatus="open", filledshares="65", unfilledshares="65", averageprice="60.4", quantity="130")]
    got = r.broker.get_order("1001")
    assert (got.status, got.filled_qty, got.filled_lots, got.avg_price) == (PARTIAL, 65, 1, 60.4)


def test_order_book_is_cached_to_respect_the_rate_limit(tmp_path):
    r = Rig(tmp_path)
    r.place()
    r.client.book = [r.row()]
    r.broker.get_order("1001"); r.broker.get_order("1001"); r.broker.open_orders()
    assert r.client.book_calls == 1
    r.now += 1.2
    r.broker.get_order("1001")
    assert r.client.book_calls == 2


def test_lost_place_reply_is_reconciled_against_the_book(tmp_path):
    r = Rig(tmp_path)
    r.client.place_result = None                                             # reply lost / no id returned...
    r.client.book = [r.row(orderid="2002")]                                  # ...but the order IS live
    assert r.place().order_id == "2002"                                      # adopted, not duplicated
    r2 = Rig(tmp_path / "b")
    r2.client.place_result = ConnectionError("timeout")
    r2.client.book = []                                                      # nothing live: genuine failure
    o = r2.place()
    assert o.status == REJECTED and o.order_id == "NONE"


def test_cancel_and_cancel_failure(tmp_path):
    r = Rig(tmp_path)
    r.place()
    r.broker.cancel_order("1001")
    assert r.client.cancelled == [("1001", "NORMAL")]
    r.client.cancel_resp = {"status": False, "message": "too late"}
    with pytest.raises(BrokerError, match="failed"):
        r.broker.cancel_order("1001")


def test_net_positions_parsing(tmp_path):
    r = Rig(tmp_path)
    r.client.positions = [{"symboltoken": "40037", "netqty": "65"}, {"symboltoken": "40038", "netqty": "-130"},
                          {"symboltoken": "1", "netqty": "0"}, {"symboltoken": "2", "netqty": "garbage"}]
    assert r.broker.net_positions() == {"40037": 65, "40038": -130}
