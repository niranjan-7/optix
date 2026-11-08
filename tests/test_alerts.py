import io
import json
import logging
import urllib.error
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

from tests.exec_helpers import HEALTHY, TUE, World, leg
from tests.test_risk import SICK
from trader import alerts as A
from trader.alerts import (
    CRITICAL, INFO, REPLY, WARNING, AlertsConfig, Alerts, AlertSender, CommandHandler, CommandPoller, TelegramApi,
    TelegramError, build_alerts, fmt_decision, fmt_event, load_alerts_config, parse_chat_id, urllib_transport,
)
from trader.config import Settings
from trader.greeks import IST
from trader.journal import Journal
from trader.risk import Decision, Rejection

CHAT = 424242
TOKEN = "123456789:AA-fake-token-for-tests-only"


class FakeTelegram:
    """Stands in for Telegram's HTTP API. Records calls; can be told to fail or to hand back updates."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[str] = []
        self.updates: list[dict] = []
        self.errors: list[dict] = []          # responses returned (in order) instead of success
        self.raise_next: list[Exception] = []

    def __call__(self, method, payload, timeout):
        self.calls.append((method, payload))
        if self.raise_next:
            raise self.raise_next.pop(0)
        if self.errors:
            return self.errors.pop(0)
        if method == "sendMessage":
            self.sent.append(payload["text"])
            return {"ok": True, "result": {}}
        if method == "getUpdates":
            off = payload.get("offset")
            if off == -1:
                return {"ok": True, "result": self.updates[-1:]}
            out = [u for u in self.updates if off is None or u["update_id"] >= off]
            return {"ok": True, "result": out}
        if method == "getMe":
            return {"ok": True, "result": {"username": "my_trading_bot"}}
        return {"ok": True, "result": {}}


def err(code, desc="boom", retry_after=None):
    d = {"ok": False, "error_code": code, "description": desc}
    if retry_after:
        d["parameters"] = {"retry_after": retry_after}
    return d


def cfg(**kw):
    return AlertsConfig(state_file=kw.pop("state_file", "/nonexistent-dir-xyz/state.json"), **kw)


class Mono:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class Rig:
    def __init__(self, tmp_path, live=False, state=None, **cfg_kw):
        self.tg = FakeTelegram()
        self.mono = Mono()
        self.sleeps: list[float] = []
        self.cfg = cfg(state_file=str(state or tmp_path / "alerts_state.json"), **cfg_kw)
        self.api = TelegramApi(self.tg)
        self.sender = AlertSender(self.api, CHAT, self.cfg, self.mono, self.sleeps.append)
        self.now = TUE
        self.alerts = Alerts(self.sender, self.cfg, live=live, clock=lambda: self.now, mono=self.mono)

    def flush(self):
        """Send everything queued; return only the messages that went out since the last flush."""
        before = len(self.tg.sent)
        self.sender.drain()
        return self.tg.sent[before:]


def decision(approved=True, codes=(("NO_STOP_LOSS", "no stop-loss"),), order=None):
    return Decision(approved, () if approved else tuple(Rejection(c, d) for c, d in codes), order)


def sig(w=None, **kw):
    w = w or World.__new__(World)
    from tests.test_risk import leg
    from trader.signals import Signal
    base = dict(idempotency_key="k1", strategy="example_momentum", kind="ENTRY", legs=(leg(),), reason="future moved +30 pts",
                created_at=TUE, stop_loss=75.2, target=140.35)
    base.update(kw)
    return Signal(**base)


# ================================================================ Telegram client

def test_api_payloads_and_errors():
    tg = FakeTelegram()
    api = TelegramApi(tg)
    api.send_message(CHAT, "hello")
    api.get_updates(None, 0); api.get_updates(7, 25)
    assert tg.calls[0] == ("sendMessage", {"chat_id": CHAT, "text": "hello", "disable_web_page_preview": True})
    assert "offset" not in tg.calls[1][1] and tg.calls[2][1]["offset"] == 7 and tg.calls[2][1]["allowed_updates"] == ["message"]
    tg.errors = [err(429, "Too Many Requests: retry after 5", 5), err(403, "Forbidden: bot was blocked")]
    with pytest.raises(TelegramError) as e:
        api.send_message(CHAT, "x")
    assert e.value.retry_after == 5 and e.value.code == 429
    with pytest.raises(TelegramError, match="blocked"):
        api.send_message(CHAT, "x")


def test_overlong_text_is_truncated():
    tg = FakeTelegram()
    TelegramApi(tg).send_message(CHAT, "x" * 10_000)
    assert len(tg.sent[0]) == 3500


def test_transport_reads_telegrams_json_error_bodies_and_never_leaks_the_token(monkeypatch):
    body = json.dumps(err(401, "Unauthorized: invalid token specified")).encode()

    def http_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))
    monkeypatch.setattr(urllib.request, "urlopen", http_error)
    t = urllib_transport(TOKEN)
    assert t("getMe", {}, 5)["error_code"] == 401                       # parsed from the 4xx body

    def down(req, timeout):
        raise urllib.error.URLError(f"cannot connect to {req.full_url}")  # such errors may contain the URL
    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(TelegramError) as e:
        t("sendMessage", {"chat_id": 1, "text": "x"}, 5)
    assert TOKEN not in str(e.value) and "URLError" in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__          # the URL-bearing original is not chained

    class Garbage:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"<html>not json</html>"
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Garbage())
    with pytest.raises(TelegramError, match="not JSON"):
        t("getMe", {}, 5)


def test_the_token_goes_into_the_request_url_only(monkeypatch):
    seen = {}

    class OK:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok":true,"result":{"username":"b"}}'

    def fake(req, timeout):
        seen.update(url=req.full_url, data=req.data, method=req.get_method(), timeout=timeout)
        return OK()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert TelegramApi(urllib_transport(TOKEN)).get_me() == {"username": "b"}
    assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/getMe" and seen["method"] == "POST"
    assert TOKEN.encode() not in seen["data"]


# ================================================================== the sender

def test_messages_are_sent_in_order(tmp_path):
    r = Rig(tmp_path)
    for i in range(3):
        r.sender.submit(INFO, f"m{i}")
    assert r.flush() == ["m0", "m1", "m2"] and r.sender.sent == 3


def test_flood_control_holds_back_only_routine_alerts(tmp_path):
    r = Rig(tmp_path, max_messages_per_minute=6, burst=3)
    for i in range(6):
        r.sender.submit(INFO, f"routine{i}")                           # only 3 fit in the burst
    r.sender.submit(WARNING, "a warning"); r.sender.submit(CRITICAL, "FIRE"); r.sender.submit(REPLY, "reply to you")
    sent = r.flush()
    joined = "\n".join(sent)
    assert all(f"routine{i}" in joined for i in (0, 1, 2)) and not any(f"routine{i}" in joined for i in (3, 4, 5))
    assert "a warning" in joined and "FIRE" in joined and "reply to you" in sent       # these are never held back
    # the note is on the first message AFTER the gap (the warning), where the gap actually happened
    assert sent[3].startswith("(+3 routine alerts held back to avoid flooding)\na warning")
    assert sent[4] == "FIRE" and r.sender.suppressed == 0


def test_bounded_queue_drops_old_routine_alerts_first_when_telegram_is_unreachable(tmp_path):
    r = Rig(tmp_path, max_queue=10, max_messages_per_minute=10_000, burst=10_000)
    r.sender.submit(CRITICAL, "keep me")
    for i in range(30):
        r.sender.submit(INFO, f"r{i}")
    assert len(r.sender._q) == 10 and ("CRITICAL", "keep me") in list(r.sender._q)
    assert r.sender.failed == 21


def test_rate_limited_by_telegram_waits_as_told_then_succeeds(tmp_path):
    r = Rig(tmp_path)
    r.tg.errors = [err(429, "Too Many Requests", 7)]
    r.sender.submit(INFO, "hello")
    assert r.flush() == ["hello"] and r.sleeps == [7]


def test_transient_failures_back_off_then_give_up_and_say_so(tmp_path, caplog):
    r = Rig(tmp_path)
    r.tg.raise_next = [TelegramError("could not reach Telegram (OSError)")] * 4
    r.sender.submit(WARNING, "important")
    with caplog.at_level(logging.ERROR):
        assert r.flush() == []
    assert r.sleeps == [1, 2, 4, 8] and r.sender.failed == 1 and "Gave up sending" in caplog.text


@pytest.mark.parametrize("code", [400, 401, 403])
def test_permanent_errors_are_not_retried(tmp_path, code, caplog):
    r = Rig(tmp_path)
    r.tg.errors = [err(code, "chat not found")]
    r.sender.submit(INFO, "x")
    with caplog.at_level(logging.ERROR):
        r.flush()
    assert r.sleeps == [] and r.sender.failed == 1 and "TELEGRAM_CHAT_ID" in caplog.text


def test_background_thread_delivers_and_stops_cleanly(tmp_path):
    r = Rig(tmp_path)
    r.sender.start()
    r.sender.submit(INFO, "async hello")
    import time
    for _ in range(100):
        if r.tg.sent:
            break
        time.sleep(0.02)
    r.sender.submit(INFO, "last words")
    r.sender.stop()
    assert r.tg.sent == ["async hello", "last words"]


def test_a_broken_transport_never_raises_into_the_caller(tmp_path):
    r = Rig(tmp_path)
    r.tg.raise_next = [RuntimeError("kaboom")]
    r.sender.submit(INFO, "x")
    r.flush()                                                           # swallowed and counted
    assert r.sender.failed == 1


# ================================================================== formatting

def test_approved_entry_message_has_the_numbers(tmp_path):
    from trader.risk import RiskAssessment
    order = SimpleNamespace(assessment=RiskAssessment("LONG_OPTION", 100.3, 1628.25, 6519.5))
    level, text, key = fmt_decision(sig(), decision(True, order=order))
    assert level == INFO and key == ""
    for needle in ("SIGNAL APPROVED 10:30:00", "example_momentum · ENTRY · BUY 1 lot (65) NIFTY25000CE",
                   "stop 75.2 · target 140.35 · risk at stop Rs 1,628", "Why: future moved +30 pts"):
        assert needle in text, needle


def test_refusal_message_lists_every_reason_and_has_a_dedupe_key():
    d = decision(False, (("KILL_SWITCH", "kill switch is ON"), ("NO_STOP_LOSS", "no stop-loss")))
    level, text, key = fmt_decision(sig(source="webhook", idempotency_key="webhook:abc"), d)
    assert "SIGNAL REFUSED" in text and "• KILL_SWITCH: kill switch is ON" in text and "• NO_STOP_LOSS: no stop-loss" in text
    assert "Source: webhook" in text and "webhook:abc" in text and key == "example_momentum|ENTRY|KILL_SWITCH,NO_STOP_LOSS"


def test_exit_approval_has_no_stop_line():
    _, text, _ = fmt_decision(sig(kind="EXIT", stop_loss=None, target=None, reason="target hit"), decision(True))
    assert "stop" not in text.lower().replace("stop-loss", "") and "Why: target hit" in text


def test_event_formatting_and_levels():
    c = AlertsConfig()
    assert fmt_event({"type": "order_placed", "side": "BUY", "lots": 1, "symbol": "S", "limit_price": 101.3}, c) == (INFO, "📤 ORDER BUY 1× S at limit 101.3")
    lv, txt = fmt_event({"type": "fill", "side": "SELL", "lots": 1, "symbol": "S", "price": 99.7, "fee": 20.0}, c)
    assert lv == INFO and "FILL SELL 1× S @ 99.7" in txt and "Rs 20" in txt
    lv, txt = fmt_event({"type": "position_closed", "position_id": "P1", "realised_pnl": -79.0, "fees": 40.0, "strategy": "ex"}, c)
    assert "P1 CLOSED" in txt and "Rs -79" in txt and "after fees Rs 40" in txt
    assert fmt_event({"type": "warning", "message": "no quote"}, c)[0] == WARNING
    for msg in ("FILL NOT BOOKED for X. CHECK POSITIONS AT BROKER", "POSITION MISMATCH with broker", "square-off rejected. CLOSE IT MANUALLY"):
        assert fmt_event({"type": "warning", "message": msg}, c)[0] == CRITICAL, msg
    assert fmt_event({"type": "execution_started"}, c) is None and fmt_event({"type": "mystery"}, c) is None
    off = AlertsConfig(send_orders=False, send_fills=False)
    assert fmt_event({"type": "order_placed", "side": "BUY", "lots": 1, "symbol": "S", "limit_price": 1}, off) is None
    assert fmt_event({"type": "fill", "side": "BUY", "lots": 1, "symbol": "S", "price": 1, "fee": 0}, off) is None
    assert fmt_event({"type": "warning", "message": "still sent"}, off) is not None          # safety messages cannot be switched off


# ================================================================ the facade

def test_nothing_happens_and_nothing_breaks_when_telegram_is_not_set_up():
    a = Alerts(None, AlertsConfig())
    assert not a.enabled
    a.on_decision(sig(), decision(True)); a.on_event({"type": "fill"}); a.risk_notice("CRITICAL", "x"); a.tick()
    assert a.maybe_daily_summary() is False
    a.start(); a.stop()


def test_messages_are_tagged_paper_or_live(tmp_path):
    p = Rig(tmp_path)
    p.alerts.send(INFO, "hello")
    assert p.flush() == ["[PAPER] hello"]
    l = Rig(tmp_path / "l", live=True)
    l.alerts.send(INFO, "hello")
    assert l.flush() == ["[LIVE] hello"]


def test_identical_refusals_are_folded_together(tmp_path):
    r = Rig(tmp_path, repeat_window_seconds=60)
    for _ in range(4):
        r.alerts.on_decision(sig(), decision(False))
    assert len([m for m in r.flush() if "REFUSED" in m]) == 1
    r.mono.t += 61
    r.alerts.on_decision(sig(), decision(False))
    last = r.flush()[-1]
    assert "REFUSED" in last and "the same refusal happened 3 more times" in last
    r.alerts.on_decision(sig(), decision(False, (("DUPLICATE", "dup"),)))     # a different reason is never folded away
    assert "DUPLICATE" in r.flush()[-1]


def test_identical_warnings_are_folded_but_never_dropped_by_flood_control(tmp_path):
    r = Rig(tmp_path, max_messages_per_minute=1, burst=1)
    for i in range(3):
        r.alerts.send(INFO, f"noise{i}")                                # exhausts the routine budget
    for _ in range(4):
        r.alerts.on_event({"type": "warning", "message": "no market quote for X"})
    out = r.flush()
    assert len([m for m in out if "no market quote for X" in m]) == 1       # folded, not repeated
    r.mono.t += 61
    r.alerts.on_event({"type": "warning", "message": "no market quote for X"})
    assert "happened 3 more times" in r.flush()[0]
    r.alerts.on_event({"type": "warning", "message": "a different problem"})
    assert "a different problem" in r.flush()[0]


def test_signal_and_refusal_alerts_can_be_switched_off_independently(tmp_path):
    r = Rig(tmp_path, send_signals=False)
    r.alerts.on_decision(sig(), decision(True)); r.alerts.on_decision(sig(), decision(False))
    assert [("REFUSED" in m) for m in r.flush()] == [True]
    r2 = Rig(tmp_path / "b", send_rejections=False)
    r2.alerts.on_decision(sig(), decision(True)); r2.alerts.on_decision(sig(), decision(False))
    assert ["APPROVED" in m for m in r2.flush()] == [True]


def test_risk_notices_are_critical_and_always_delivered(tmp_path):
    r = Rig(tmp_path, max_messages_per_minute=1, burst=1)
    for i in range(5):
        r.alerts.send(INFO, f"noise{i}")
    r.alerts.risk_notice("CRITICAL", "KILL SWITCH ON by telegram: test")
    r.alerts.risk_notice("WARNING", "Kill switch RESET by uncle")
    sent = r.flush()
    assert any("🚨 KILL SWITCH ON by telegram: test" in m for m in sent) and any("RESET by uncle" in m for m in sent)


def test_a_listener_bug_never_reaches_the_caller(tmp_path):
    r = Rig(tmp_path)
    r.alerts.on_event({"type": "fill"})                              # missing keys: would KeyError inside the formatter
    r.alerts.on_decision(None, None)
    r.alerts.risk_notice("CRITICAL", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- feed watching

def feed_rig(tmp_path, healthy=False, positions=0, **kw):
    r = Rig(tmp_path, **kw)
    state = {"h": HEALTHY if healthy else SICK}
    r.alerts.bind(health=lambda: state["h"], open_positions=lambda: positions, weekdays=(0, 1, 2, 3, 4))
    r.state = state
    return r


def test_feed_trouble_alerts_only_after_the_threshold_during_market_hours(tmp_path):
    r = feed_rig(tmp_path)
    r.alerts.check_feed()
    r.mono.t += 14; r.alerts.check_feed()
    assert r.flush() == []                                           # 14 s < 15 s
    r.mono.t += 2; r.alerts.check_feed()
    (msg,) = r.flush()
    assert "unhealthy for 16s" in msg and "New entries are blocked" in msg and "not connected" in msg
    r.mono.t += 100; r.alerts.check_feed()
    assert r.flush() == []                                           # no repeat before the reminder interval
    r.mono.t += 250; r.alerts.check_feed()
    assert "unhealthy" in r.flush()[0]                               # reminder after 300 s
    r.state["h"] = HEALTHY; r.mono.t += 30; r.alerts.check_feed()
    assert "recovered" in r.flush()[0]
    r.alerts.check_feed()
    assert r.flush() == []


def test_feed_down_with_open_positions_is_critical_and_points_at_kill(tmp_path):
    r = feed_rig(tmp_path, positions=2)
    r.alerts.check_feed(); r.mono.t += 20; r.alerts.check_feed()
    (msg,) = r.flush()
    assert "MARKET DATA DOWN" in msg and "2 open position(s)" in msg and "NOT being watched" in msg and "/kill" in msg


def test_a_flapping_feed_that_recovers_quickly_is_silent(tmp_path):
    r = feed_rig(tmp_path)
    r.alerts.check_feed(); r.mono.t += 5
    r.state["h"] = HEALTHY; r.alerts.check_feed()
    r.state["h"] = SICK; r.mono.t += 5; r.alerts.check_feed()
    assert r.flush() == []


@pytest.mark.parametrize("when", [datetime(2026, 11, 10, 8, 0, tzinfo=IST), datetime(2026, 11, 10, 16, 0, tzinfo=IST),
                                  datetime(2026, 11, 14, 11, 0, tzinfo=IST)])
def test_no_feed_alerts_when_the_market_is_closed(tmp_path, when):
    r = feed_rig(tmp_path)
    r.now = when
    r.alerts.check_feed(); r.mono.t += 3600; r.alerts.check_feed()
    assert r.flush() == []                                           # silence overnight and on weekends is normal


# ---------------------------------------------------------------- daily summary

def trade_world(tmp_path):
    w = World(tmp_path / "w")
    j = Journal(tmp_path / "j.db")
    w.risk.add_listener(j.on_decision); w.mgr.add_listener(j.on_event)
    w.mgr.submit(w.entry("e1")); w.advance(1)
    w.mgr.submit(w.exit("x1")); w.advance(1)
    w.mgr.submit(w.entry("bad", sl=None))
    return w, j


def test_daily_summary_content_and_one_per_day(tmp_path):
    w, j = trade_world(tmp_path)
    r = Rig(tmp_path / "a")
    r.alerts.bind(health=lambda: HEALTHY, open_positions=lambda: 0, weekdays=(0, 1, 2, 3, 4), journal=j,
                  portfolio=w.portfolio, risk=w.risk)
    r.now = datetime(2026, 11, 10, 15, 44, tzinfo=IST)
    assert r.alerts.maybe_daily_summary() is False                  # too early
    r.now = datetime(2026, 11, 10, 15, 45, tzinfo=IST)
    assert r.alerts.maybe_daily_summary() is True and r.alerts.maybe_daily_summary() is False
    (msg,) = r.flush()
    for needle in ("DAILY SUMMARY Tue 10-Nov-2026", "Signals: 3 seen, 2 approved, 1 refused", "NO_STOP_LOSS",
                   "Closed trades: 1 (won 0, lost 1", "Net P&L (after fees): Rs -79", "1 trade(s) need your evening review",
                   "Open positions: 0", "Orders used", "kill switch off"):
        assert needle in msg, needle


def test_summary_is_not_resent_after_a_restart_and_skips_weekends(tmp_path):
    state = tmp_path / "state.json"
    r1 = Rig(tmp_path / "a", state=state)
    r1.alerts.bind(health=lambda: HEALTHY, open_positions=lambda: 0, weekdays=(0, 1, 2, 3, 4))
    r1.now = datetime(2026, 11, 10, 16, 0, tzinfo=IST)
    assert r1.alerts.maybe_daily_summary()
    r2 = Rig(tmp_path / "b", state=state)                            # "restart": new object, same state file
    r2.alerts.bind(health=lambda: HEALTHY, open_positions=lambda: 0, weekdays=(0, 1, 2, 3, 4))
    r2.now = datetime(2026, 11, 10, 17, 0, tzinfo=IST)
    assert r2.alerts.maybe_daily_summary() is False
    r2.now = datetime(2026, 11, 11, 15, 50, tzinfo=IST)              # next trading day: sent again
    assert r2.alerts.maybe_daily_summary() is True
    r2.now = datetime(2026, 11, 14, 16, 0, tzinfo=IST)               # Saturday
    assert r2.alerts.maybe_daily_summary() is False


def test_a_quiet_day_still_gets_a_summary_as_a_sign_of_life(tmp_path):
    j = Journal(tmp_path / "j.db")
    r = Rig(tmp_path / "a")
    r.alerts.bind(health=lambda: HEALTHY, open_positions=lambda: 0, weekdays=(0, 1, 2, 3, 4), journal=j)
    r.now = datetime(2026, 11, 10, 15, 50, tzinfo=IST)
    r.alerts.tick()
    assert "No trades closed today" in r.flush()[0]


# ================================================================ phone commands

def handler_rig(tmp_path, healthy=True, **kw):
    w = World(tmp_path / "w")
    r = Rig(tmp_path / "a")
    r.alerts.bind(health=lambda: HEALTHY if healthy else SICK, open_positions=lambda: len(w.portfolio.open_positions()),
                  weekdays=(0, 1, 2, 3, 4), portfolio=w.portfolio, risk=w.risk)
    codes = iter(["111111", "222222", "333333"])
    h = CommandHandler(allowed_chat_id=CHAT, cfg=r.cfg, risk=w.risk, portfolio=w.portfolio, alerts=r.alerts,
                       health=lambda: HEALTHY if healthy else SICK, code_gen=lambda: next(codes), mono=r.mono)
    r.w, r.h = w, h
    return r


NOW = 1_800_000_000.0


def update(text, uid=1, chat_id=CHAT, chat_type="private", from_id=CHAT, is_bot=False, age=1.0, username="uncle", key="message"):
    return {"update_id": uid, key: {"message_id": uid, "date": NOW - age, "text": text,
                                    "chat": {"id": chat_id, "type": chat_type},
                                    "from": {"id": from_id, "is_bot": is_bot, "username": username}}}


@pytest.mark.parametrize("kw", [
    dict(chat_id=999, from_id=999),                    # a stranger who found the bot
    dict(chat_id=CHAT, from_id=999),                   # right chat, different sender
    dict(chat_id=-CHAT, chat_type="group", from_id=CHAT),   # the bot added to a group
    dict(chat_type="supergroup"), dict(chat_type="channel"),
    dict(is_bot=True),                                 # another bot
    dict(key="edited_message"), dict(key="channel_post"),
    dict(age=61.0),                                    # stale: could be a replay
    dict(age=3600.0),
])
def test_only_the_owner_in_a_private_chat_with_a_fresh_message_gets_obeyed(tmp_path, kw):
    r = handler_rig(tmp_path)
    r.w.mgr.submit(r.w.entry("e1")); r.w.advance(1)
    assert r.h.handle_update(update("/kill", **kw), NOW) is None
    assert not r.w.risk.status()["kill_active"] and len(r.w.portfolio.open_positions()) == 1


def test_malformed_updates_are_ignored(tmp_path):
    r = handler_rig(tmp_path)
    for u in ({}, {"update_id": 1}, {"message": "nope"}, {"message": {"chat": {"id": CHAT, "type": "private"}}},
              update("/status") | {"message": None}, update("just chatting"), update("")):
        assert r.h.handle_update(u, NOW) is None


def test_status_positions_summary_and_help(tmp_path):
    r = handler_rig(tmp_path)
    s = r.h.handle_update(update("/status"), NOW)
    for needle in ("[PAPER] STATUS", "Kill switch: off", "Day halted: no", "Feed: healthy", "P&L today:", "Orders 0/5 · positions 0/2"):
        assert needle in s, needle
    assert r.h.handle_update(update("/positions"), NOW) == "No open positions."
    r.w.mgr.submit(r.w.entry("e1")); r.w.advance(1)
    p = r.h.handle_update(update("/positions"), NOW)
    assert "P1 (ex): BUY 1× NIFTY25000CE" in p and "stop 75.2" in p
    assert "DAILY SUMMARY" in r.h.handle_update(update("/summary"), NOW)
    assert "/kill" in r.h.handle_update(update("/help"), NOW) and "/reset" in r.h.handle_update(update("/start"), NOW)
    assert "Unknown command" in r.h.handle_update(update("/launch_missiles"), NOW)
    assert "Feed: UNHEALTHY" in handler_rig(tmp_path / "x", healthy=False).h.handle_update(update("/status"), NOW)


def test_command_names_tolerate_case_and_bot_suffix(tmp_path):
    r = handler_rig(tmp_path)
    assert "STATUS" in r.h.handle_update(update("/Status@my_trading_bot"), NOW)


def test_kill_stops_everything_records_who_and_why(tmp_path):
    r = handler_rig(tmp_path)
    r.w.mgr.submit(r.w.entry("e1")); r.w.advance(1)
    reply = r.h.handle_update(update("/kill fat finger on the strike", username="uncle"), NOW)
    assert "KILL SWITCH ON" in reply
    st = r.w.risk.status()
    assert st["kill_active"] and st["kill_reason"] == "fat finger on the strike" and st["kill_source"] == "telegram:uncle"
    r.w.advance(2)
    assert r.w.portfolio.open_positions() == []                      # the position was really closed
    assert "Kill switch: ON" in r.h.handle_update(update("/status"), NOW)
    r2 = handler_rig(tmp_path / "y")
    r2.h.handle_update(update("/kill"), NOW)
    assert r2.w.risk.status()["kill_reason"] == "telegram /kill"


def test_kill_failure_is_reported_not_hidden(tmp_path):
    r = handler_rig(tmp_path)
    r.w.risk.kill = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broker down"))
    reply = r.h.handle_update(update("/kill"), NOW)
    assert "failed" in reply and "command line" in reply and "broker down" not in reply


def test_reset_needs_a_one_time_code_and_a_reason(tmp_path):
    r = handler_rig(tmp_path)
    assert "nothing to reset" in r.h.handle_update(update("/reset"), NOW)       # kill not on
    r.w.risk.kill("test")
    ask = r.h.handle_update(update("/reset"), NOW)
    assert "/reset 111111 <why it is safe>" in ask and "120 seconds" in ask
    assert r.w.risk.status()["kill_active"]                                    # asking does not reset anything
    assert "wrong or has expired" in r.h.handle_update(update("/reset 999999 looks fine to me"), NOW)
    assert r.w.risk.status()["kill_active"]
    # the wrong guess also burns the code: it must be requested again
    assert "wrong or has expired" in r.h.handle_update(update("/reset 111111 looks fine to me"), NOW)
    r.h.handle_update(update("/reset"), NOW)                                    # new code: 222222
    assert "add a reason" in r.h.handle_update(update("/reset 222222 ok"), NOW)
    assert r.w.risk.status()["kill_active"]
    ok = r.h.handle_update(update("/reset 222222 checked positions and the feed"), NOW)
    assert "reset by uncle" in ok and not r.w.risk.status()["kill_active"]
    assert r.w.mgr.submit(r.w.entry("again")).approved                          # trading is armed again
    r.w.risk.kill("again")
    assert "wrong or has expired" in r.h.handle_update(update("/reset 222222 reusing an old code"), NOW)   # single use


def test_reset_code_expires(tmp_path):
    r = handler_rig(tmp_path)
    r.w.risk.kill("t")
    r.h.handle_update(update("/reset"), NOW)
    r.mono.t += 121
    assert "wrong or has expired" in r.h.handle_update(update("/reset 111111 safe after checking"), NOW)
    assert r.w.risk.status()["kill_active"]


def test_odd_input_to_reset_cannot_crash_or_bypass_it(tmp_path):
    r = handler_rig(tmp_path)
    r.w.risk.kill("t"); r.h.handle_update(update("/reset"), NOW)
    for text in ("/reset ٣٣٣٣٣٣ because", "/reset  ", "/reset 111111", "/reset 1111111 long reason here", "/reset -- --"):
        out = r.h.handle_update(update(text), NOW)
        assert out and r.w.risk.status()["kill_active"], text


def test_reset_does_not_lift_the_daily_loss_halt_and_says_so(tmp_path):
    r = handler_rig(tmp_path)
    r.w.portfolio.record_entry_fill(None, "ex", None, None, leg(), 1, 1000.0, True)       # bought at 1000; market is ~100: a big loss
    r.w.risk.poll()
    assert r.w.risk.status()["day_halted"]
    r.w.risk.kill("t"); r.h.handle_update(update("/reset"), NOW)
    assert "entries stay blocked until tomorrow" in r.h.handle_update(update("/reset 111111 checked everything twice"), NOW)


# ---------------------------------------------------------------- the poller

def poller_rig(tmp_path):
    r = handler_rig(tmp_path)
    p = CommandPoller(r.api, r.h, r.sender, CHAT, r.cfg, clock=lambda: NOW, sleep=r.sleeps.append)
    r.p = p
    return r


def test_a_backlog_waiting_in_telegram_is_discarded_never_executed(tmp_path):
    r = poller_rig(tmp_path)
    r.tg.updates = [update("/kill", uid=10, age=5), update("/reset 111111 old", uid=11, age=5)]   # received while we were offline
    r.p.drain_backlog()
    assert r.p._offset == 12
    assert r.p.poll_once() == 0
    assert not r.w.risk.status()["kill_active"] and r.tg.sent == []


def test_new_commands_after_start_are_executed_once_and_answered_immediately(tmp_path):
    r = poller_rig(tmp_path)
    r.p.drain_backlog()
    r.tg.updates = [update("/status", uid=1)]
    assert r.p.poll_once() == 1
    assert len(r.tg.sent) == 1 and "STATUS" in r.tg.sent[0]                      # sent without waiting for the sender thread
    assert r.p.poll_once() == 0 and len(r.tg.sent) == 1                          # the same update is not handled twice
    assert r.tg.calls[-1][1]["offset"] == 2


def test_unauthorised_senders_get_no_reply_at_all(tmp_path):
    r = poller_rig(tmp_path)
    r.tg.updates = [update("/status", uid=1, chat_id=999, from_id=999)]
    assert r.p.poll_once() == 1 and r.tg.sent == []                              # does not even confirm the bot exists


def test_offset_advances_even_if_a_command_blows_up(tmp_path):
    r = poller_rig(tmp_path)
    r.h.handle_update = lambda u, now: (_ for _ in ()).throw(RuntimeError("bug"))
    r.tg.updates = [update("/kill", uid=5)]
    with pytest.raises(RuntimeError):
        r.p.poll_once()
    assert r.p._offset == 6                                                      # a crashing command is not retried forever


def test_run_loop_survives_telegram_errors_and_hints_at_conflicts(tmp_path, caplog):
    r = poller_rig(tmp_path)
    calls = {"n": 0}
    real = r.api.get_updates

    def flaky(offset, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(offset, timeout)                                         # drain_backlog
        if calls["n"] == 2:
            raise TelegramError("Telegram said: Conflict: terminated by other getUpdates request", code=409)
        r.p._stop.set()
        return []
    r.api.get_updates = flaky
    with caplog.at_level(logging.WARNING):
        r.p._run()
    assert r.sleeps == [2] and "another program is using this bot" in caplog.text


def test_if_the_backlog_cannot_be_cleared_commands_stay_off(tmp_path, caplog):
    r = poller_rig(tmp_path)
    r.api.get_updates = lambda offset, timeout: (_ for _ in ()).throw(TelegramError("could not reach Telegram (OSError)"))
    with caplog.at_level(logging.ERROR):
        r.p._run()                                                               # returns instead of looping
    assert "commands stay OFF" in caplog.text


# ============================================================= whole pipeline

def test_a_full_round_trip_produces_the_alerts_you_would_want(tmp_path):
    w = World(tmp_path / "w")
    r = Rig(tmp_path / "a", burst=50, max_messages_per_minute=600)
    w.risk.add_listener(r.alerts.on_decision); w.mgr.add_listener(r.alerts.on_event); w.risk.set_notifier(r.alerts.risk_notice)
    w.mgr.submit(w.entry("e1")); w.advance(1)
    w.mgr.submit(w.exit("x1")); w.advance(1)
    w.mgr.submit(w.entry("nosl", sl=None))
    w.risk.kill("because", "telegram:uncle")
    kinds = []
    for m in r.flush():
        for tag in ("SIGNAL APPROVED", "ORDER", "FILL", "POSITION P1 CLOSED", "SIGNAL REFUSED", "KILL SWITCH ON"):
            if tag in m:
                kinds.append(tag)
    assert kinds == ["SIGNAL APPROVED", "ORDER", "FILL", "SIGNAL APPROVED", "ORDER", "FILL", "POSITION P1 CLOSED",
                     "SIGNAL REFUSED", "KILL SWITCH ON"]
    assert all(m.startswith("[PAPER] ") for m in r.tg.sent)
    assert any("KILL SWITCH ON by telegram:uncle: because" in m for m in r.tg.sent)


# ============================================================ assembly and CLI

def settings(token="", chat=""):
    return Settings(_env_file=None, telegram_bot_token=token, telegram_chat_id=chat)


def test_alerts_are_off_not_broken_without_credentials(caplog):
    for s, why in ((settings(), "not set"), (settings(TOKEN, ""), "not set"), (settings("", "123"), "not set"),
                   (settings(TOKEN, "not-a-number"), "must be a number")):
        with caplog.at_level(logging.WARNING):
            alerts, api, chat = build_alerts(s)
        assert not alerts.enabled and api is None and chat is None and why in caplog.records[-1].getMessage()
        assert TOKEN not in caplog.text
    off, api, _ = build_alerts(settings(TOKEN, "5"), AlertsConfig(enabled=False))
    assert not off.enabled and api is None


def test_alerts_are_on_with_credentials_and_commands_need_no_extra_setup():
    tg = FakeTelegram()
    alerts, api, chat = build_alerts(settings(TOKEN, " 424242 "), transport=tg)
    assert alerts.enabled and chat == 424242 and isinstance(api, TelegramApi)
    assert A.attach_commands(alerts, api, chat, risk=None, portfolio=None, health=lambda: HEALTHY) is not None
    assert A.attach_commands(Alerts(None, AlertsConfig()), api, chat, risk=None, portfolio=None, health=lambda: HEALTHY) is None
    assert A.attach_commands(alerts.__class__(alerts._sender, AlertsConfig(accept_commands=False)), api, chat,
                             risk=None, portfolio=None, health=lambda: HEALTHY) is None


def test_chat_id_parsing():
    assert [parse_chat_id(x) for x in ("123", " 456 ", "-100123", "abc", "", "12.5")] == [123, 456, -100123, None, None, None]


def test_shipped_alerts_yaml_loads_and_rejects_mistakes(tmp_path):
    c = load_alerts_config("config/alerts.yaml")
    assert c.enabled and c.max_messages_per_minute == 20 and c.command_max_age_seconds == 60 and c.reset_code_ttl_seconds == 120
    raw = yaml.safe_load(open("config/alerts.yaml"))
    for bad in ({"daily_summary_time": 1545}, {"typo_field": 1}, {"max_messages_per_minute": 0}, {"burst": 0}):
        with pytest.raises(ValueError):
            AlertsConfig(**{**raw, **bad})


def cli(monkeypatch, args, tg, token=TOKEN, chat="424242"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)
    out: list[str] = []
    code = A._cli(args, transport=tg, out=out.append)
    return code, "\n".join(out)


def test_cli_test_sends_a_message_and_never_prints_the_token(monkeypatch):
    tg = FakeTelegram()
    code, out = cli(monkeypatch, ["test"], tg)
    assert code == 0 and "@my_trading_bot" in out and "424242" in out and TOKEN not in out
    assert "Alerts are working" in tg.sent[0]


def test_cli_whoami_lists_private_chats_so_you_can_find_your_id(monkeypatch):
    tg = FakeTelegram()
    tg.updates = [update("hi", uid=1, chat_id=777, from_id=777, username="uncle"),
                  update("again", uid=2, chat_id=777, from_id=777, username="uncle"),
                  update("x", uid=3, chat_id=-5, chat_type="group", from_id=777)]
    code, out = cli(monkeypatch, ["whoami"], tg, chat="")
    assert code == 0 and out.count("Chat ID 777") == 1 and "-5" not in out and "TELEGRAM_CHAT_ID=<number>" in out
    code, out = cli(monkeypatch, ["whoami"], FakeTelegram(), chat="")
    assert code == 1 and "send any message" in out.lower()


def test_cli_problems_are_explained(monkeypatch):
    assert cli(monkeypatch, ["test"], FakeTelegram(), token="")[0] == 2
    code, out = cli(monkeypatch, ["test"], FakeTelegram(), chat="")
    assert code == 2 and "whoami" in out
    bad = FakeTelegram(); bad.errors = [err(401, "Unauthorized: invalid token specified")]
    code, out = cli(monkeypatch, ["test"], bad)
    assert code == 1 and "Unauthorized" in out and TOKEN not in out
