import logging
from datetime import datetime, time, timedelta, timezone

import pytest
import yaml

from trader.auth import AuthError
from trader.config import Settings
from trader.greeks import IST
from trader.ops import (
    DailyRestart, Heartbeat, OpsLoop, OpsConfig, TokenKeeper, load_ops_config, next_restart_after,
)

URL = "https://hc-ping.example.com/3f9a1c2e-SECRET-UUID-7b1d"


class Mono:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


class Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, n=-1): return b"OK"


class Opener:
    def __init__(self): self.calls, self.fail = [], None
    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.fail:
            raise self.fail
        return Resp()


# ================================================================= heartbeat

def test_heartbeat_pings_at_its_interval_and_not_before():
    op, m = Opener(), Mono()
    hb = Heartbeat(URL, 60, op, m)
    assert hb.tick() is True and len(op.calls) == 1               # first tick pings straight away
    m.t += 59; assert hb.tick() is False
    m.t += 2; assert hb.tick() is True and len(op.calls) == 2 and hb.pings == 2
    assert op.calls[0] == (URL, 5)                                # short timeout: a hung watchdog must not hang us


def test_heartbeat_is_off_without_a_url():
    op = Opener()
    hb = Heartbeat("", 60, op, Mono())
    assert not hb.enabled and hb.tick() is False and op.calls == []


def test_heartbeat_failures_never_raise_and_never_log_the_secret_url(caplog):
    op, m = Opener(), Mono()
    hb = Heartbeat(URL, 60, op, m)
    op.fail = OSError(f"connection to {URL} refused")             # exception text can contain the URL
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            hb.tick(); m.t += 61
        op.fail = None
        hb.tick()
    text = caplog.text
    assert "SECRET-UUID" not in text and "hc-ping.example.com" not in text
    assert "Heartbeat ping failed (OSError); 1 in a row" in text and "working again after 3 failure" in text
    assert text.count("ping failed") == 1                          # not one line per failure


def test_heartbeat_url_is_registered_as_a_secret_for_log_redaction():
    s = Settings(_env_file=None, heartbeat_url=URL)
    assert URL in s.secret_values()


# ============================================================ daily restart time

@pytest.mark.parametrize("start,expected", [
    (datetime(2026, 11, 10, 6, 0, tzinfo=IST), datetime(2026, 11, 10, 8, 30, tzinfo=IST)),    # before: later today
    (datetime(2026, 11, 10, 8, 30, tzinfo=IST), datetime(2026, 11, 11, 8, 30, tzinfo=IST)),   # exactly at: tomorrow
    (datetime(2026, 11, 10, 20, 0, tzinfo=IST), datetime(2026, 11, 11, 8, 30, tzinfo=IST)),   # after: tomorrow
    (datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc), datetime(2026, 11, 10, 8, 30, tzinfo=IST)),   # 07:30 IST given in UTC
])
def test_next_restart_after(start, expected):
    assert next_restart_after(start, time(8, 30)) == expected


class Restart:
    def __init__(self, started, busy=False, alert_every=1800):
        self.now = started
        self.busy = busy
        self.requests, self.notes = 0, []
        self.m = Mono()
        self.dr = DailyRestart(time(8, 30), started, lambda: self.now, lambda: self.busy,
                               lambda: setattr(self, "requests", self.requests + 1),
                               lambda lv, msg: self.notes.append((lv, msg)), alert_every, self.m)


def test_restart_waits_for_the_time_then_happens_once():
    r = Restart(datetime(2026, 11, 10, 20, 0, tzinfo=IST))
    r.now = datetime(2026, 11, 11, 8, 29, tzinfo=IST); r.dr.tick()
    assert r.requests == 0
    r.now = datetime(2026, 11, 11, 8, 30, tzinfo=IST); r.dr.tick(); r.dr.tick(); r.dr.tick()
    assert r.requests == 1 and r.dr.requested and r.notes[0][0] == "INFO" and "Daily restart" in r.notes[0][1]


def test_a_start_after_the_restart_time_restarts_tomorrow_not_immediately():
    r = Restart(datetime(2026, 11, 10, 9, 15, tzinfo=IST))
    r.now = datetime(2026, 11, 10, 15, 0, tzinfo=IST); r.dr.tick()
    assert r.requests == 0 and r.dr.due_at == datetime(2026, 11, 11, 8, 30, tzinfo=IST)


def test_restart_never_happens_on_top_of_an_open_position_and_says_so_sparingly():
    r = Restart(datetime(2026, 11, 10, 20, 0, tzinfo=IST), busy=True, alert_every=1800)
    r.now = datetime(2026, 11, 11, 8, 31, tzinfo=IST)
    for _ in range(5):
        r.dr.tick()
    assert r.requests == 0 and [n[0] for n in r.notes] == ["WARNING"] and "POSTPONED" in r.notes[0][1]
    r.m.t += 1801; r.dr.tick()
    assert len(r.notes) == 2 and r.requests == 0                   # reminded again after the interval, still no restart
    r.busy = False; r.dr.tick()
    assert r.requests == 1                                         # restarts by itself once flat


# ================================================================ token keeper

def test_token_keeper_refreshes_on_its_interval_and_survives_failures():
    calls, notes, m = [], [], Mono()

    class Auth:
        fail = False
        def ensure_session(self):
            calls.append(1)
            if self.fail:
                raise AuthError("TOTP rejected")
    a = Auth()
    tk = TokenKeeper(a, 60, lambda lv, msg: notes.append((lv, msg)), m)
    tk.tick(); tk.tick()
    assert len(calls) == 1
    m.t += 61; tk.tick()
    assert len(calls) == 2 and notes == []
    a.fail = True
    for _ in range(4):
        m.t += 61; tk.tick()                                        # must not raise
    assert len(notes) == 1 and "login refresh failed" in notes[0][1] and "TOTP rejected" in notes[0][1]   # not 4 alerts
    m.t += 601; tk.tick()
    assert len(notes) == 2


def test_one_failing_ops_task_does_not_stop_the_others(caplog):
    ran = []

    class Bad:
        def tick(self): raise RuntimeError("bug")

    class Good:
        def tick(self): ran.append(1)
    OpsLoop([Bad(), None, Good()]).tick()
    assert ran == [1] and "ops task Bad failed" in caplog.text


# ===================================================================== config

def test_shipped_ops_yaml_loads_and_mistakes_are_refused():
    c = load_ops_config("config/ops.yaml")
    assert c.daily_restart == time(8, 30) and c.heartbeat_seconds == 60
    raw = yaml.safe_load(open("config/ops.yaml"))
    for bad in ({"daily_restart": 830}, {"typo": 1}, {"heartbeat_seconds": 0}):
        with pytest.raises(ValueError):
            OpsConfig(**{**raw, **bad})
