import json
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.exec_helpers import TUE, leg
from tests.test_instruments import sample_rows
from tests.test_strategy import INDEX_ROW
from trader import runtime
from trader.auth import AuthError
from trader.brokers import AngelBroker, PaperBroker
from trader.config import Settings
from trader.feed import AngelFeed, SimulatedFeed
from trader.greeks import IST
from trader.ops import DailyRestart, Heartbeat, TokenKeeper
from trader.signals import Signal

TODAY = date(2026, 11, 10)
SECRET = "correct-horse-battery-staple-123"
PHRASE = "START LIVE C1"


@pytest.fixture(autouse=True)
def small_master(tmp_path, monkeypatch):
    """Use a tiny instrument file instead of downloading the real 38 MB one."""
    path = tmp_path / "master.json"
    path.write_text(json.dumps(sample_rows() + [INDEX_ROW]))
    calls = []

    def fake(cache_dir, today=None, **kw):
        calls.append(today)
        return path
    monkeypatch.setattr(runtime, "ensure_master", fake)
    return calls


def settings(**kw):
    base = dict(webhook_secret=SECRET)
    base.update(kw)
    return Settings(_env_file=None, **base)


ANGEL = dict(angel_api_key="KEY12345", angel_client_id="C1", angel_pin="9876", angel_totp_secret="JBSWY3DPEHPK3PXP")


class FakeAuth:
    client = SimpleNamespace()

    def __init__(self, s=None, fail=False):
        self.fail, self.logins, self.ensures = fail, 0, 0

    def login(self):
        self.logins += 1
        if self.fail:
            raise AuthError("TOTP rejected")

    def ensure_session(self):
        self.ensures += 1


def build(tmp_path, s=None, **kw):
    kw.setdefault("feed_mode", "simulated")
    kw.setdefault("start", False)
    return runtime.build_system(s or settings(), data_dir=tmp_path / "d", today=TODAY, clock=lambda: TUE, **kw)


# ================================================================ paper default

def test_default_is_paper_on_simulated_prices_and_never_prompts(tmp_path):
    def boom(prompt): raise AssertionError("paper mode must never ask for a live confirmation")
    sys_ = build(tmp_path, input_fn=boom)
    assert (sys_.mode, sys_.data_source, sys_.live) == ("PAPER", "simulated", False)
    assert isinstance(sys_.broker, PaperBroker) and isinstance(sys_.feed, SimulatedFeed) and sys_.auth is None
    assert sys_.expiry == date(2026, 11, 17)                          # the lapsed 09-Nov expiry is skipped


def test_everything_is_wired_so_a_signal_is_judged_and_journalled(tmp_path):
    s = build(tmp_path)
    sig = Signal("k1", "ex", "ENTRY", (leg(),), "wired?", TUE, stop_loss=75.2)
    d = s.mgr.submit(sig)
    assert not d.approved                                              # no quote yet: the engine says no, for a reason
    (row,) = s.journal.recent_signals(5)
    assert row["strategy"] == "ex" and row["approved"] is False and row["codes"]
    assert s.risk._actions is s.mgr                                    # kill switch can actually act
    assert not s.alerts.enabled                                        # no Telegram configured: harmless


def test_the_scheduler_and_threads_start_and_stop_cleanly(tmp_path):
    s = build(tmp_path, start=True)
    assert any(t.name == "scheduler" for t in threading.enumerate())
    s.stop()


# ============================================================== real prices, paper orders

def test_real_prices_need_credentials_and_fail_before_any_download(tmp_path, small_master):
    with pytest.raises(SystemExit) as e:
        build(tmp_path, feed_mode="live")
    assert "ANGEL_API_KEY" in str(e.value) and "--feed simulated" in str(e.value)
    assert small_master == []                                          # nothing was downloaded before failing


def test_real_prices_with_paper_orders(tmp_path):
    auth = FakeAuth()
    sys_ = build(tmp_path, settings(**ANGEL), feed_mode="live", auth_factory=lambda s: auth,
                 socket_factory=lambda sess, st: object())
    assert (sys_.mode, sys_.data_source) == ("PAPER", "angel")
    assert isinstance(sys_.feed, AngelFeed) and isinstance(sys_.broker, PaperBroker)    # real data, still paper orders
    assert auth.logins == 1                                            # logged in up front
    assert any(isinstance(t, TokenKeeper) for t in sys_.ops._tasks)    # and kept fresh


def test_a_bad_broker_login_stops_start_up_with_a_clear_message(tmp_path):
    with pytest.raises(SystemExit, match="login failed: TOTP rejected"):
        build(tmp_path, settings(**ANGEL), feed_mode="live", auth_factory=lambda s: FakeAuth(fail=True))


# ======================================================= live orders: the two-key gate

def live_settings():
    return settings(live_trading=True, **ANGEL)


def test_real_orders_on_simulated_prices_are_refused_before_anything_happens(tmp_path, small_master):
    with pytest.raises(SystemExit, match="prices are SIMULATED"):
        build(tmp_path, live_settings(), feed_mode="simulated")
    assert small_master == []


def test_live_mode_cannot_start_unattended(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))      # e.g. a Docker restart loop
    with pytest.raises(SystemExit, match="Live trading was NOT enabled.*unattended"):
        build(tmp_path, live_settings(), feed_mode="live", auth_factory=FakeAuth, socket_factory=lambda a, b: object())


@pytest.mark.parametrize("typed", ["", "yes", "start live c1", "START LIVE"])
def test_live_mode_needs_the_exact_phrase(tmp_path, typed):
    with pytest.raises(SystemExit, match="Live trading was NOT enabled"):
        build(tmp_path, live_settings(), feed_mode="live", auth_factory=FakeAuth,
              socket_factory=lambda a, b: object(), input_fn=lambda p: typed)


def test_both_keys_turn_live_mode_on_and_it_is_labelled_everywhere(tmp_path):
    sys_ = build(tmp_path, live_settings(), feed_mode="live", auth_factory=FakeAuth,
                 socket_factory=lambda a, b: object(), input_fn=lambda p: PHRASE)
    assert sys_.live and sys_.mode == "LIVE" and isinstance(sys_.broker, AngelBroker)
    assert "LIVE TRADING: REAL ORDERS" in runtime.banner(sys_, "127.0.0.1", 8000)
    assert sys_.alerts.tag == "[LIVE]"


def test_live_env_flag_alone_never_enables_live_orders(tmp_path):
    """LIVE_TRADING=false (the default) with credentials present: paper broker, no prompt, no matter what."""
    sys_ = build(tmp_path, settings(**ANGEL), feed_mode="live", auth_factory=FakeAuth,
                 socket_factory=lambda a, b: object(), input_fn=lambda p: PHRASE)
    assert sys_.mode == "PAPER" and not isinstance(sys_.broker, AngelBroker)


def test_banner_says_what_mode_and_which_prices(tmp_path):
    sim = build(tmp_path)
    assert "PAPER MODE" in runtime.banner(sim, "127.0.0.1", 8000) and "SIMULATED" in runtime.banner(sim, "127.0.0.1", 8000)
    real = build(tmp_path / "b", settings(**ANGEL), feed_mode="live", auth_factory=FakeAuth, socket_factory=lambda a, b: object())
    assert "REAL (Angel One)" in runtime.banner(real, "127.0.0.1", 8000) and "http://127.0.0.1:8000/" in runtime.banner(real, "127.0.0.1", 8000)


# ============================================================== operations wiring

def test_daily_restart_waits_for_a_flat_book(tmp_path):
    now = {"t": TUE}
    s = build(tmp_path)
    s.ops._tasks = [t for t in s.ops._tasks if isinstance(t, DailyRestart)]
    dr = s.ops._tasks[0]
    dr._clock = lambda: datetime(2026, 11, 11, 8, 31, tzinfo=IST)
    s.portfolio.record_entry_fill(None, "ex", None, None, leg(), 1, 100.0, True)       # a position is open
    s.ops.tick()
    assert not s.restart_event.is_set()                                                # postponed
    s.portfolio._positions.clear()
    s.ops.tick()
    assert s.restart_event.is_set()                                                    # flat: restarts


def test_heartbeat_is_only_added_when_a_url_is_configured(tmp_path):
    assert not any(isinstance(t, Heartbeat) for t in build(tmp_path).ops._tasks)
    with_url = build(tmp_path / "b", settings(heartbeat_url="https://hc.example.com/ping/abc-123-def"))
    assert any(isinstance(t, Heartbeat) for t in with_url.ops._tasks)


# ===================================================================== main()

def test_main_flags_and_clean_shutdown(tmp_path, monkeypatch):
    import uvicorn
    seen = {}

    class Cfg:
        def __init__(self, app, **kw): seen["cfg"] = kw

    class Srv:
        should_exit = False
        def __init__(self, cfg): pass
        def run(self): seen["ran"] = True

    monkeypatch.setattr(uvicorn, "Config", Cfg)
    monkeypatch.setattr(uvicorn, "Server", Srv)
    monkeypatch.setattr(runtime, "load_settings", lambda: settings())
    orig = runtime.build_system
    built = {}

    def spy(*a, **k):
        built["sys"] = orig(*a, **{**k, "start": False})
        return built["sys"]
    monkeypatch.setattr(runtime, "build_system", spy)
    orig_app = runtime.create_app
    monkeypatch.setattr(runtime, "create_app", lambda s, m, sub, cfg, **kw: (seen.update(peers=cfg.trusted_proxy_peers),
                                                                              orig_app(s, m, sub, cfg, **kw))[1])
    runtime.main(["--feed", "simulated", "--data-dir", str(tmp_path / "d")])
    assert seen["cfg"]["host"] == "127.0.0.1" and seen["ran"]                   # default: this computer only
    assert "*" not in seen["peers"]                                             # and no blanket trust of proxy headers
    runtime.main(["--feed", "simulated", "--data-dir", str(tmp_path / "d2"), "--host", "0.0.0.0", "--trust-proxy-from-any-peer"])
    assert seen["cfg"]["host"] == "0.0.0.0" and seen["peers"] == ["*"]


def test_main_refuses_to_start_with_a_weak_webhook_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "load_settings", lambda: settings(webhook_secret="short"))
    with pytest.raises(SystemExit, match="Cannot start"):
        runtime.main(["--feed", "simulated", "--data-dir", str(tmp_path)])


def test_module_entry_point_shows_help():
    r = subprocess.run([sys.executable, "-m", "trader", "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--feed" in r.stdout and "paper mode by default" in r.stdout


def test_the_price_source_defaults_to_live_and_can_be_set_in_the_env_file(monkeypatch):
    assert Settings(_env_file=None).trader_feed == "live"
    assert Settings(_env_file=None, trader_feed="simulated").trader_feed == "simulated"
    with pytest.raises(ValueError):
        Settings(_env_file=None, trader_feed="paper")                      # typos are refused, not guessed at
    monkeypatch.setenv("TRADER_FEED", "simulated")
    assert Settings(_env_file=None).trader_feed == "simulated"             # how Docker's env_file delivers it


def test_banner_never_shows_an_unbrowsable_address(tmp_path):
    b = runtime.banner(build(tmp_path), "0.0.0.0", 8000)              # what Docker uses
    assert "http://127.0.0.1:8000/" in b and "0.0.0.0" not in b
