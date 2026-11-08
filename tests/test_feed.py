import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trader.auth import AuthError
from trader.feed import (
    AngelFeed, ChainTracker, FeedConfig, FeedHealth, Instr, SimulatedFeed, Tick,
    load_feed_config, parse_angel_message,
)
from trader.instruments import InstrumentMaster
from tests.test_instruments import sample_rows

NFO = lambda t: Instr("NFO", t)


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def mk_tick(token="1", exch="NFO", ltp=100.0):
    return Tick(exch, token, ltp, ltp - 0.5, ltp + 0.5, None, None, None, 0.0)


# ------------------------------------------------------------------ health

def test_health_unhealthy_until_connected_and_ticking():
    c = Clock(); h = FeedHealth(10, clock=c)
    assert not h.is_healthy() and "not connected" in h.status().reason
    h.set_connected(True)
    assert "no ticks" in h.status().reason
    h.record_tick(NFO("1"))
    assert h.is_healthy()


def test_health_goes_stale_and_recovers():
    c = Clock(); h = FeedHealth(10, clock=c)
    h.set_connected(True); h.record_tick(NFO("1"))
    c.t += 9.9; assert h.is_healthy()
    c.t += 0.2
    st = h.status()
    assert not st.healthy and "no tick for" in st.reason
    h.record_tick(NFO("1")); assert h.is_healthy()


def test_old_tick_before_reconnect_does_not_count():
    c = Clock(); h = FeedHealth(10, clock=c)
    h.set_connected(True); h.record_tick(NFO("1"))
    h.set_connected(False, "boom"); c.t += 1
    h.set_connected(True)
    assert not h.is_healthy() and "since (re)connect" in h.status().reason


def test_disconnect_reason_reported():
    h = FeedHealth(10, clock=Clock())
    h.set_connected(True); h.record_tick(NFO("1")); h.set_connected(False, "socket closed")
    assert "socket closed" in h.status().reason


def test_critical_instrument_stale_even_if_others_tick():
    c = Clock(); h = FeedHealth(10, clock=c)
    h.set_critical([NFO("FUT")])
    h.set_connected(True); h.record_tick(NFO("FUT"))
    for _ in range(3):
        c.t += 6; h.record_tick(NFO("OPT"))
    st = h.status()
    assert not st.healthy and "NFO:FUT" in st.reason


# ----------------------------------------------------------------- parsing

def msg(**kw):
    base = {"exchange_type": 2, "token": "40037", "last_traded_price": 12345,
            "exchange_timestamp": 1_700_000_000_000,
            "best_5_buy_data": [{"price": 12300, "quantity": 5}, {"price": 12250, "quantity": 9}],
            "best_5_sell_data": [{"price": 12400, "quantity": 5}, {"price": 12450, "quantity": 1}],
            "volume_trade_for_the_day": 777, "open_interest": 55}
    base.update(kw); return base


def test_parse_converts_paise_and_picks_best_levels():
    t = parse_angel_message(msg(), 5.0)
    assert (t.exchange, t.token, t.ltp, t.bid, t.ask) == ("NFO", "40037", 123.45, 123.0, 124.0)
    assert t.spread == pytest.approx(1.0) and t.exchange_ts == 1_700_000_000.0 and t.volume == 777


def test_parse_ltp_only_message_has_no_quotes():
    t = parse_angel_message({"exchange_type": 1, "token": "99926000", "last_traded_price": 2500000}, 0)
    assert t.exchange == "NSE" and t.ltp == 25000.0 and t.bid is None and t.spread is None


def test_parse_crossed_book_drops_quotes():
    t = parse_angel_message(msg(best_5_buy_data=[{"price": 13000}], best_5_sell_data=[{"price": 12000}]), 0)
    assert t.ltp == 123.45 and t.bid is None and t.ask is None


@pytest.mark.parametrize("bad", [
    msg(last_traded_price=0), msg(exchange_type=99), {"token": "1"}, msg(last_traded_price="x"),
])
def test_parse_rejects_garbage(bad):
    assert parse_angel_message(bad, 0) is None


# -------------------------------------------------------------- feed base

def test_bad_callback_does_not_break_others_and_health_updates():
    f = SimulatedFeed({}, health=FeedHealth(10))
    got = []
    f.on_tick(lambda t: 1 / 0)
    f.on_tick(got.append)
    f.health.set_connected(True)
    f._emit(mk_tick())
    assert len(got) == 1 and f.health.is_healthy()


# --------------------------------------------------------------- simulated

def test_simulated_deterministic_and_subscription_filtered():
    prices = {NFO("1"): 100.0, NFO("2"): 50.0}
    a = SimulatedFeed(prices, seed=7); b = SimulatedFeed(prices, seed=7)
    for f in (a, b):
        f.set_subscriptions([NFO("1")]); f.health.set_connected(True)
    ta, tb = a.step(1.0), b.step(1.0)
    assert [t.ltp for t in ta] == [t.ltp for t in tb]
    assert [t.token for t in ta] == ["1"]  # token 2 not subscribed
    assert ta[0].bid < ta[0].ltp < ta[0].ask


def test_simulated_outage_makes_feed_unhealthy():
    f = SimulatedFeed({NFO("1"): 100.0}, seed=1)
    f.set_subscriptions([NFO("1")]); f.health.set_connected(True); f.step()
    assert f.health.is_healthy()
    f.pause()
    assert f.step() == [] and not f.health.is_healthy()
    f.resume(); f.step()
    assert f.health.is_healthy()


def test_simulated_thread_runs_and_stops():
    f = SimulatedFeed({NFO("1"): 100.0}, seed=1, tick_interval=0.01)
    got = []; f.on_tick(got.append); f.set_subscriptions([NFO("1")])
    f.start(); time.sleep(0.15); f.stop()
    assert len(got) >= 3 and not f.health.is_healthy()


# ------------------------------------------------------------ Angel (fake)

class FakeSocket:
    """Stands in for SmartWebSocketV2. `script` runs inside connect()."""
    instances = []

    def __init__(self, script=None):
        self.script, self.subs, self.unsubs, self.closed = script, [], [], False
        self.block = threading.Event()
        FakeSocket.instances.append(self)

    def connect(self):
        self.on_open(None)
        if self.script: self.script(self)
        if getattr(self, "hold", False):
            self.block.wait(5)

    def subscribe(self, cid, mode, tl): self.subs.append((mode, tl))
    def unsubscribe(self, cid, mode, tl): self.unsubs.append((mode, tl))
    def close_connection(self): self.closed = True; self.block.set()


class FakeAuth:
    def __init__(self, fail=False):
        self.fail, self.logins, self.ensures = fail, 0, 0
    def login(self):
        self.logins += 1
        if self.fail: raise AuthError("nope")
        return SimpleNamespace(jwt_token="j", feed_token="f")
    def ensure_session(self):
        self.ensures += 1
        if self.fail: raise AuthError("nope")
        return SimpleNamespace(jwt_token="j", feed_token="f")


SETTINGS = SimpleNamespace(angel_api_key=SimpleNamespace(get_secret_value=lambda: "k"), angel_client_id="C")


def mk_angel(factory, auth=None, sleep=lambda d: None, cfg=None):
    FakeSocket.instances = []
    return AngelFeed(auth or FakeAuth(), SETTINGS, cfg or FeedConfig(),
                     health=FeedHealth(10), socket_factory=factory, sleep=sleep)


def flat(tl):
    return sorted((e["exchangeType"], t) for e in tl for t in e["tokens"])


def test_on_open_subscribes_with_right_modes():
    f = mk_angel(lambda s, st: FakeSocket())
    f.set_subscriptions([NFO("10"), NFO("11"), Instr("NSE", "99926000")])
    f._connect_once(False)
    modes = {m: flat(tl) for m, tl in FakeSocket.instances[0].subs}
    assert modes == {3: [(2, "10"), (2, "11")], 1: [(1, "99926000")]}  # F&O snap-quote, index LTP


def test_live_subscription_diff_sent_only_when_open():
    f = mk_angel(lambda s, st: FakeSocket())
    f.set_subscriptions([NFO("1")])  # before any socket: just recorded
    holder = {}
    def script(sock):
        holder["s"] = sock
        f.set_subscriptions([NFO("1"), NFO("2")])
        f.set_subscriptions([NFO("2")])
    f._factory = lambda s, st: FakeSocket(script)
    f._connect_once(False)
    s = FakeSocket.instances[0]
    assert [flat(tl) for _, tl in s.subs] == [[(2, "1")], [(2, "2")]]
    assert [flat(tl) for _, tl in s.unsubs] == [[(2, "1")]]


def test_reconnect_resubscribes_everything_and_backs_off():
    delays = []
    f = mk_angel(lambda s, st: FakeSocket(), sleep=lambda d: (delays.append(d), len(delays) >= 3 and f._stop.set()))
    f.set_subscriptions([NFO("1"), NFO("2")])
    f._run()
    assert len(FakeSocket.instances) == 3
    for s in FakeSocket.instances:  # every new socket got the full set again
        assert flat([e for _, tl in s.subs for e in tl]) == [(2, "1"), (2, "2")]
    assert delays == [1, 2, 4]  # exponential backoff while no ticks arrive


def test_backoff_resets_after_a_connection_that_ticked():
    n = {"i": 0}
    def factory(s, st):
        n["i"] += 1
        # 3rd connection delivers a tick before dying
        return FakeSocket(lambda sock: f._on_data(None, msg()) if n["i"] == 3 else None)
    delays = []
    f = mk_angel(factory, sleep=lambda d: (delays.append(d), len(delays) >= 4 and f._stop.set()))
    f._run()
    assert delays == [1, 2, 1, 1]  # after a healthy connection, backoff starts over


def test_relogin_forced_after_two_failures():
    auth = FakeAuth()
    f = mk_angel(lambda s, st: FakeSocket(), auth=auth,
                 sleep=lambda d: auth.logins >= 1 and f._stop.set())
    f._run()
    assert auth.ensures == 2 and auth.logins == 1


def test_auth_failure_is_survived_and_reported():
    f = mk_angel(lambda s, st: FakeSocket(), auth=FakeAuth(fail=True))
    assert f._connect_once(False) is False
    assert "login failed" in f.health.status().reason and FakeSocket.instances == []


def test_factory_exception_does_not_crash_supervisor():
    def boom(s, st): raise RuntimeError("sdk exploded")
    f = mk_angel(boom)
    assert f._connect_once(False) is False
    assert not f.health.is_healthy()


def test_ticks_flow_to_callbacks_and_health():
    f = mk_angel(lambda s, st: FakeSocket(lambda sock: f._on_data(None, msg())))
    got = []; f.on_tick(got.append)
    seen = {}
    f.on_tick(lambda t: seen.setdefault("healthy", f.health.is_healthy()))
    assert f._connect_once(False) is True
    assert got[0].ltp == 123.45 and seen["healthy"] is True


def test_watchdog_drops_silent_socket():
    c = Clock()
    f = mk_angel(lambda s, st: FakeSocket(), cfg=FeedConfig(stale_seconds=5, reconnect_after_seconds=20))
    f.health = FeedHealth(5, clock=c)
    sock = FakeSocket(); f._sock = sock; f._open = True
    f.health.set_connected(True)
    c.t += 19; assert f._watchdog_check() is False and not sock.closed
    c.t += 2;  assert f._watchdog_check() is True and sock.closed
    assert not f.health.is_healthy()


def test_threaded_start_stop_with_real_blocking_socket():
    def factory(s, st):
        sock = FakeSocket(); sock.hold = True; return sock
    f = mk_angel(factory, sleep=None)
    f.set_subscriptions([NFO("1")])
    f.start()
    deadline = time.time() + 2
    while not FakeSocket.instances and time.time() < deadline: time.sleep(0.01)
    time.sleep(0.05)
    assert FakeSocket.instances and f.health.status().connected
    f.stop()
    assert FakeSocket.instances[0].closed and not f.health.status().connected
    assert all(not t.is_alive() for t in f._threads)


# ---------------------------------------------------------------- ChainTracker

@pytest.fixture
def tracker_env():
    rows = sample_rows() + [{
        "token": "99926000", "name": "NIFTY", "expiry": "", "symbol": "Nifty 50", "strike": "0",
        "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0"}]
    m = InstrumentMaster.from_rows(rows, {"NIFTY"})
    feed = SimulatedFeed({}, health=FeedHealth(10))
    tr = ChainTracker(feed, m, "NIFTY", date(2026, 11, 9), 2, date(2026, 11, 8))
    return m, feed, tr


def test_tracker_starts_with_underlyings_only(tracker_env):
    _, feed, tr = tracker_env
    assert feed.subscriptions() == {tr.future, tr.index} and tr.index == Instr("NSE", "99926000")


def test_tracker_subscribes_window_on_first_spot_and_rolls(tracker_env):
    m, feed, tr = tracker_env
    feed._emit(Tick("NSE", "99926000", 25001.0, None, None, None, None, None, 0))
    assert tr.atm == 25000 and len(feed.subscriptions()) == 2 + 5 * 2  # 5 strikes x CE/PE + fut + idx
    before = feed.subscriptions()
    feed._emit(Tick("NSE", "99926000", 25010.0, None, None, None, None, None, 0))
    assert feed.subscriptions() == before  # same ATM: no churn
    feed._emit(Tick("NSE", "99926000", 25060.0, None, None, None, None, None, 0))
    assert tr.atm == 25050 and feed.subscriptions() != before


def test_tracker_ignores_future_once_spot_known(tracker_env):
    _, feed, tr = tracker_env
    feed._emit(Tick("NSE", "99926000", 25001.0, None, None, None, None, None, 0))
    feed._emit(Tick("NFO", tr.future.token, 25200.0, None, None, None, None, None, 0))  # basis premium
    assert tr.atm == 25000


def test_pinned_positions_survive_window_roll_until_unpinned(tracker_env):
    m, feed, tr = tracker_env
    feed._emit(Tick("NSE", "99926000", 25001.0, None, None, None, None, None, 0))
    pos = Instr("NFO", m.find_option("NIFTY", date(2026, 11, 9), 24900, "CE").token)
    tr.pin(pos)
    tr.recenter(25250.0)  # window now far from 24900
    assert pos in feed.subscriptions()
    tr.unpin(pos)
    assert pos not in feed.subscriptions()


def test_tracker_marks_future_critical(tracker_env):
    _, feed, tr = tracker_env
    feed.health.set_connected(True)
    feed._emit(Tick("NSE", "99926000", 25001.0, None, None, None, None, None, 0))
    assert "critical" in feed.health.status().reason  # future never ticked


# ------------------------------------------------------------------ config

def test_feed_yaml_loads():
    cfg = load_feed_config("config/feed.yaml")
    assert cfg.underlying == "NIFTY" and cfg.quote_mode == "snap_quote"


@pytest.mark.parametrize("bad", [
    dict(strikes_each_side=0), dict(stale_seconds=0), dict(quote_mode="depth20"),
    dict(stale_seconds=30, reconnect_after_seconds=10), dict(typo_field=1),
])
def test_feed_config_rejects_bad_values(bad):
    with pytest.raises(ValidationError):
        FeedConfig(**bad)
