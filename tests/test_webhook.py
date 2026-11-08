import hashlib
import hmac as hmac_mod
import json
import logging
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.exec_helpers import TUE, World
from tests.test_instruments import sample_rows
from tests.test_strategy import INDEX_ROW
from trader.config import ConfigError, Settings
from trader.greeks import IST
from trader.instruments import InstrumentMaster
from trader.risk import Decision, Rejection
from trader.webhook import (
    AuthFailureGuard, TokenBucket, WebhookConfig, WebhookSignal, create_app,
)

SECRET = "correct-horse-battery-staple-123"
HMAC_SECRET = "another-long-secret-for-signing-9"
MASTER = InstrumentMaster.from_rows(sample_rows() + [INDEX_ROW], {"NIFTY"})

GOOD = {"idempotency_id": "tv-0001-abcd", "symbol": "NIFTY", "action": "BUY", "strike": 25000,
        "option_type": "CE", "qty": 65, "stop_loss": 75.2, "target": 140.0}
H = {"X-Webhook-Secret": SECRET}


def settings(secret=SECRET, hmac_secret=""):
    return Settings(_env_file=None, webhook_secret=secret, webhook_hmac_secret=hmac_secret)


class Mono:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class Rig:
    def __init__(self, hmac_secret="", cfg=None, submit=None):
        self.signals = []
        self.mono = Mono()

        def default_submit(sig):
            self.signals.append(sig)
            return Decision(True, (), None)

        self.submit = submit or default_submit
        self.app = create_app(settings(hmac_secret=hmac_secret), MASTER, self.submit, cfg or WebhookConfig(),
                              clock=lambda: TUE, monotonic=self.mono)
        self.c = TestClient(self.app)

    def post(self, body=None, headers=H, raw=None, **kw):
        data = raw if raw is not None else json.dumps(GOOD if body is None else body)
        return self.c.post("/webhook", content=data, headers={"Content-Type": "application/json", **(headers or {})}, **kw)


@pytest.fixture
def rig():
    return Rig()


def sign(body: str, key=HMAC_SECRET) -> str:
    return "sha256=" + hmac_mod.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


# ===================================================================== happy path

def test_buy_becomes_an_entry_signal(rig):
    r = rig.post()
    assert r.status_code == 200 and r.json()["status"] == "approved"
    (s,) = rig.signals
    leg = s.legs[0]
    assert (s.kind, s.strategy, s.source, s.idempotency_key) == ("ENTRY", "webhook", "webhook", "webhook:tv-0001-abcd")
    assert (leg.side, leg.strike, leg.opt_type, leg.lots, leg.lot_size) == ("BUY", 25000, "CE", 1, 65)
    assert (s.stop_loss, s.target) == (75.2, 140.0)
    assert leg.expiry.isoformat() == "2026-11-17" and leg.symbol == "NIFTY17NOV2625000CE"   # nearest, not the lapsed 09-Nov


def test_exit_becomes_a_sell_to_close(rig):
    body = {k: v for k, v in GOOD.items() if k not in ("stop_loss", "target")} | {"action": "EXIT"}
    assert rig.post(body).status_code == 200
    s = rig.signals[0]
    assert s.kind == "EXIT" and s.legs[0].side == "SELL" and s.stop_loss is None


def test_multiple_lots_and_explicit_expiry(rig):
    rig.post(GOOD | {"qty": 130, "expiry": "2026-11-17"})
    assert rig.signals[0].legs[0].lots == 2


def test_risk_rejection_is_reported_not_hidden(tmp_path):
    reject = Decision(False, (Rejection("FEED_UNHEALTHY", "market data not trustworthy"),), None)
    rig = Rig(submit=lambda s: reject)
    r = rig.post()
    assert r.status_code == 200
    assert r.json() == {"status": "rejected", "idempotency_id": "tv-0001-abcd",
                        "reasons": [{"code": "FEED_UNHEALTHY", "detail": "market data not trustworthy"}]}


def test_crash_in_submit_fails_closed(caplog):
    def boom(sig): raise RuntimeError("db exploded")
    r = Rig(submit=boom).post()
    assert r.status_code == 500 and "NOT executed" in r.json()["detail"]
    assert "db exploded" not in r.text


# ====================================================================== auth

@pytest.mark.parametrize("headers", [None, {}, {"X-Webhook-Secret": "wrong"}, {"X-Webhook-Secret": ""},
                                     {"X-Webhook-Secret": SECRET + "x"}, {"X-Webhook-Secret": SECRET[:-1]}])
def test_wrong_or_missing_secret_is_401_and_nothing_runs(rig, headers):
    r = rig.post(headers=headers)
    assert r.status_code == 401 and r.json() == {"status": "error", "detail": "unauthorized"}
    assert rig.signals == []


def test_secret_in_body_works_for_tradingview_and_is_not_treated_as_a_field(rig):
    r = rig.post(GOOD | {"secret": SECRET}, headers=None)
    assert r.status_code == 200 and len(rig.signals) == 1


def test_wrong_body_secret_and_non_string_secret(rig):
    assert rig.post(GOOD | {"secret": "nope"}, headers=None).status_code == 401
    assert rig.post(GOOD | {"secret": 12345}, headers=None).status_code == 401
    assert rig.post(GOOD | {"secret": None}, headers=None).status_code == 401


def test_body_secret_can_be_disabled():
    rig = Rig(cfg=WebhookConfig(allow_body_secret=False))
    assert rig.post(GOOD | {"secret": SECRET}, headers=None).status_code == 401
    assert rig.post().status_code == 200                                 # header still works


def test_header_takes_precedence_over_a_bad_body_secret(rig):
    assert rig.post(GOOD | {"secret": "ignored"}).status_code == 200


def test_hmac_required_when_configured():
    roomy = WebhookConfig(rate_limit={"per_minute": 600, "burst": 100}, auth_failures={"max_per_minute": 100, "block_minutes": 1})
    rig = Rig(hmac_secret=HMAC_SECRET, cfg=roomy)
    raw = json.dumps(GOOD)
    assert rig.post(raw=raw).status_code == 401                                            # secret ok, signature missing
    assert rig.post(raw=raw, headers=H | {"X-Signature": "sha256=" + "0" * 64}).status_code == 401
    assert rig.post(raw=raw, headers=H | {"X-Signature": sign(raw, "wrong-key-wrong-key-xx")}).status_code == 401
    assert rig.post(raw=raw, headers={"X-Signature": sign(raw)}).status_code == 401        # signature ok, secret missing
    assert rig.post(raw=raw, headers=H | {"X-Signature": sign(raw)}).status_code == 200
    # the signature covers the exact bytes: any change to the body invalidates it
    tampered = raw.replace("65", "130")
    assert rig.post(raw=tampered, headers=H | {"X-Signature": sign(raw)}).status_code == 401
    assert len(rig.signals) == 1


def test_secrets_never_appear_in_responses_or_logs(caplog):
    rig = Rig(hmac_secret=HMAC_SECRET)
    with caplog.at_level(logging.DEBUG):
        responses = [rig.post(headers={"X-Webhook-Secret": SECRET[:-1]}), rig.post(GOOD | {"secret": SECRET}, headers=None),
                     rig.post(raw="not json", headers=H), rig.post(GOOD | {"bogus": 1}, headers=H | {"X-Signature": sign(json.dumps(GOOD | {"bogus": 1}))})]
    everything = " ".join(r.text for r in responses) + " ".join(x.getMessage() for x in caplog.records)
    assert SECRET not in everything and HMAC_SECRET not in everything and SECRET[:-1] not in everything


def test_app_refuses_to_start_with_a_weak_secret():
    for bad in ("", "short", "x" * 15):
        with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
            create_app(settings(secret=bad), MASTER, lambda s: None)
    with pytest.raises(ConfigError, match="HMAC"):
        create_app(settings(hmac_secret="tiny"), MASTER, lambda s: None)


# ================================================================ strict schema

def bad(**changes):
    return {k: v for k, v in (GOOD | changes).items() if v is not ...}


@pytest.mark.parametrize("body,field", [
    (bad(extra_field=1), "extra_field"),
    (bad(action="SELL"), "action"),
    (bad(action="buy"), "action"),
    (bad(action="Exit"), "action"),
    (bad(option_type="CALL"), "option_type"),
    (bad(option_type="ce"), "option_type"),
    (bad(qty=0), "qty"),
    (bad(qty=-65), "qty"),
    (bad(qty="65"), "qty"),
    (bad(qty=65.5), "qty"),
    (bad(strike=-1), "strike"),
    (bad(strike="25000"), "strike"),
    (bad(stop_loss=0), "stop_loss"),
    (bad(stop_loss="75"), "stop_loss"),
    (bad(target=-5), "target"),
    (bad(idempotency_id="short"), "idempotency_id"),
    (bad(idempotency_id="has spaces in it"), "idempotency_id"),
    (bad(idempotency_id="x" * 65), "idempotency_id"),
    (bad(idempotency_id=12345678), "idempotency_id"),
    (bad(symbol=""), "symbol"),
    (bad(expiry="17/11/2026"), "expiry"),
    (bad(idempotency_id=...), "idempotency_id"),
    (bad(symbol=...), "symbol"),
    (bad(action=...), "action"),
    (bad(strike=...), "strike"),
    (bad(option_type=...), "option_type"),
    (bad(qty=...), "qty"),
])
def test_strict_schema_refuses_with_a_precise_field(rig, body, field):
    r = rig.post(body)
    assert r.status_code == 422 and rig.signals == []
    assert field in [e["field"] for e in r.json()["detail"]]


def test_action_error_explains_the_fix(rig):
    msg = rig.post(bad(action="SELL")).json()["detail"][0]["message"]
    assert "BUY" in msg and "EXIT" in msg and "not supported" in msg


def test_exit_cannot_carry_stop_or_target(rig):
    assert rig.post(bad(action="EXIT")).status_code == 422


@pytest.mark.parametrize("raw,status", [
    ("not json", 400), ("", 400), ("[]", 422), ('"string"', 422), ("null", 422),
    ('{"idempotency_id": NaN}', 400), ('{"strike": Infinity}', 400),
    (json.dumps(GOOD).encode("utf-16").decode("latin-1"), 400),
])
def test_unparseable_bodies(rig, raw, status):
    assert rig.post(raw=raw).status_code == status


def test_nan_and_infinity_cannot_sneak_in_as_prices(rig):
    for v in ("NaN", "Infinity", "-Infinity"):
        raw = json.dumps(GOOD).replace("75.2", v)
        assert rig.post(raw=raw).status_code == 400
    assert rig.signals == []


def test_qty_is_units_and_must_match_the_lot_size(rig):
    for q in (1, 2, 64, 66, 100):
        r = rig.post(GOOD | {"qty": q})
        assert r.status_code == 422 and "lot size: 65 = 1 lot" in r.json()["detail"]
    assert rig.signals == []


@pytest.mark.parametrize("changes,fragment", [
    ({"symbol": "BANKNIFTY"}, "not allowed"),
    ({"symbol": "nifty "}, None),                                     # normalised: whitespace stripped, case folded
    ({"strike": 25025}, "No such option"),
    ({"strike": 99999}, "No such option"),
    ({"expiry": "2030-01-01"}, "No such option"),
])
def test_unknown_contracts_are_refused(rig, changes, fragment):
    r = rig.post(GOOD | changes)
    if fragment is None:
        assert r.status_code == 200
    else:
        assert r.status_code == 422 and fragment in r.json()["detail"]


def test_timestamp_replay_protection(rig):
    fresh = (TUE - timedelta(seconds=30)).isoformat()
    stale = (TUE - timedelta(seconds=300)).isoformat()
    future = (TUE + timedelta(seconds=300)).isoformat()
    assert rig.post(GOOD | {"timestamp": fresh}).status_code == 200
    for ts in (stale, future):
        r = rig.post(GOOD | {"idempotency_id": "tv-other-" + ts[-8:-6], "timestamp": ts})
        assert r.status_code == 422 and "timestamp" in r.json()["detail"]
    assert rig.post(GOOD | {"idempotency_id": "tv-naive-0001", "timestamp": "2026-11-10T10:29:50"}).status_code == 200   # no tz => IST


def test_oversized_body_refused(rig):
    big = json.dumps(GOOD | {"idempotency_id": "a" * 10, "pad": "x" * 6000})
    r = rig.post(raw=big)
    assert r.status_code == 413 and rig.signals == []


# ============================================================ abuse protection

def test_rate_limit_burst_then_recovery():
    rig = Rig(cfg=WebhookConfig(rate_limit={"per_minute": 6, "burst": 3}))
    codes = [rig.post(GOOD | {"idempotency_id": f"tv-burst-{i:04d}"}).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = rig.post()
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    rig.mono.t += 11                                                   # 6/min = one token per 10s
    assert rig.post(GOOD | {"idempotency_id": "tv-after-0001"}).status_code == 200


def test_brute_force_locks_the_address_even_for_the_right_secret():
    rig = Rig(cfg=WebhookConfig(auth_failures={"max_per_minute": 3, "block_minutes": 10}, rate_limit={"per_minute": 600, "burst": 100}))
    assert [rig.post(headers={"X-Webhook-Secret": f"guess{i}"}).status_code for i in range(3)] == [401, 401, 401]
    r = rig.post()                                                     # the correct secret, but this address is now blocked
    assert r.status_code == 429 and "failed attempts" in r.json()["detail"] and rig.signals == []
    rig.mono.t += 601
    assert rig.post().status_code == 200


def test_failures_are_tracked_per_address_using_the_tunnel_header_only_from_loopback():
    cfg = WebhookConfig(auth_failures={"max_per_minute": 2, "block_minutes": 10}, rate_limit={"per_minute": 600, "burst": 100})
    rig = Rig(cfg=cfg)
    for _ in range(2):                                                 # "testclient" is not loopback: header must be ignored
        rig.post(headers={"X-Webhook-Secret": "bad", "CF-Connecting-IP": "203.0.113.9"})
    # all of that counted against the real peer, so even a different claimed address is blocked
    assert rig.post(headers=H | {"CF-Connecting-IP": "198.51.100.1"}).status_code == 429


def test_token_bucket_and_failure_guard_units():
    t = Mono()
    b = TokenBucket(60, 2, t)
    assert (b.take(), b.take()) == (0.0, 0.0) and b.take() == pytest.approx(1.0)
    t.t += 1.0
    assert b.take() == 0.0
    g = AuthFailureGuard(2, 1, t)
    assert not g.record_failure("a") and g.record_failure("a") and g.blocked_for("a") > 0 and g.blocked_for("b") == 0
    t.t += 61
    assert g.blocked_for("a") == 0


# ============================================================ tunnel isolation

def test_only_webhook_and_health_are_reachable_through_the_tunnel():
    rig = Rig()

    @rig.app.get("/dashboard")
    async def dash():
        return {"secret": "internal"}

    tunnel = {"CF-Connecting-IP": "203.0.113.5", "CF-Ray": "abc"}
    assert rig.c.get("/dashboard").status_code == 200                              # local access works
    for h in (tunnel, {"X-Forwarded-For": "1.2.3.4"}):
        assert rig.c.get("/dashboard", headers=h).status_code == 404               # via the tunnel: invisible
    assert rig.c.get("/health", headers=tunnel).json() == {"status": "ok"}
    assert rig.post(headers=H | tunnel).status_code == 200
    assert rig.c.get("/docs").status_code == 404 and rig.c.get("/openapi.json").status_code == 404


def test_webhook_rejects_non_post(rig):
    assert rig.c.get("/webhook").status_code == 405


# ====================================== end to end: real risk engine + paper broker

TOK = "25000CE17"


def live_rig(tmp_path, **world_kw):
    w = World(tmp_path, px={TOK: (100.0, 0.5), "24950CE17": (100.0, 0.5)}, **world_kw)
    app = create_app(settings(), MASTER, w.mgr.submit, WebhookConfig(), clock=w.now)
    return w, TestClient(app)


def post(c, body):
    return c.post("/webhook", content=json.dumps(body), headers={"Content-Type": "application/json", **H})


def test_end_to_end_buy_then_exit_through_the_real_pipeline(tmp_path):
    w, c = live_rig(tmp_path)
    r = post(c, GOOD)
    assert r.json()["status"] == "approved"
    assert w.placed() == [("NIFTY17NOV2625000CE", "BUY", 1)]
    w.advance(1)
    (pos,) = w.portfolio.open_positions()
    assert pos.legs[0].token == TOK and pos.stop_loss == 75.2
    x = {k: v for k, v in GOOD.items() if k not in ("stop_loss", "target")} | {"action": "EXIT", "idempotency_id": "tv-exit-0001"}
    assert post(c, x).json()["status"] == "approved"
    w.advance(1)
    assert w.portfolio.open_positions() == []


def test_risk_engine_still_judges_webhook_signals(tmp_path):
    w, c = live_rig(tmp_path)
    r = post(c, bad(stop_loss=...)).json()
    assert r["status"] == "error" or r["status"] == "rejected"
    r = post(c, {k: v for k, v in GOOD.items() if k != "stop_loss"})
    assert r.json()["status"] == "rejected" and r.json()["reasons"][0]["code"] == "NO_STOP_LOSS"
    assert w.placed() == []                                                       # nothing reached the broker
    r = post(c, GOOD | {"idempotency_id": "tv-bigloss-01", "stop_loss": 40.0})    # loss at stop ~3,900 > 2,000
    assert r.json()["reasons"][0]["code"] == "MAX_LOSS_PER_TRADE"
    r = post(c, GOOD | {"idempotency_id": "tv-twolots-01", "qty": 130})
    assert "TOO_MANY_LOTS" in [x["code"] for x in r.json()["reasons"]]
    assert w.placed() == []


def test_replayed_alert_is_a_duplicate(tmp_path):
    w, c = live_rig(tmp_path)
    assert post(c, GOOD).json()["status"] == "approved"
    again = post(c, GOOD).json()
    assert again["status"] == "rejected" and again["reasons"][0]["code"] == "DUPLICATE"
    assert len(w.placed()) == 1                                                   # one order, not two


def test_exit_with_nothing_to_close_cannot_become_a_short(tmp_path):
    w, c = live_rig(tmp_path)
    x = {k: v for k, v in GOOD.items() if k not in ("stop_loss", "target")} | {"action": "EXIT", "idempotency_id": "tv-exit-0002"}
    r = post(c, x).json()
    assert r["status"] == "rejected" and r["reasons"][0]["code"] == "EXIT_NOT_COVERED"
    assert w.placed() == []


def test_kill_switch_blocks_webhook_entries_but_not_exits(tmp_path):
    w, c = live_rig(tmp_path)
    post(c, GOOD); w.advance(1)
    w.risk.kill("test")
    w.advance(2)
    assert w.portfolio.open_positions() == []                                     # kill closed it
    r = post(c, GOOD | {"idempotency_id": "tv-after-kill-1"}).json()
    assert r["status"] == "rejected" and r["reasons"][0]["code"] == "KILL_SWITCH"


def test_payload_model_is_the_documented_one():
    assert set(WebhookSignal.model_fields) == {"idempotency_id", "symbol", "action", "strike", "option_type",
                                               "qty", "stop_loss", "target", "stop_loss_pct", "target_pct",
                                               "expiry", "timestamp"}


def test_size_cap_also_applies_to_bodies_that_do_not_declare_a_length(rig):
    """Chunked upload (no Content-Length): the cap must be enforced while reading."""
    chunks = iter([b'{"pad": "' + b"x" * 3000, b"x" * 3000 + b'"}'])
    r = rig.c.post("/webhook", content=chunks, headers={"Content-Type": "application/json", **H})
    assert r.status_code == 413 and rig.signals == []


# ---------------------------------------------- percentage stop / target (TradingView-friendly)

PCT = {k: v for k, v in GOOD.items() if k not in ("stop_loss", "target")} | {"stop_loss_pct": 25, "target_pct": 40}


def pct_rig():
    sigs = []
    from trader.feed import Tick
    q = lambda tok: Tick("NFO", tok, 100.0, 99.75, 100.25, None, None, None, 0.0) if tok == "25000CE17" else None
    app = create_app(settings(), MASTER, lambda s: (sigs.append(s), Decision(True, (), None))[1], WebhookConfig(),
                     clock=lambda: TUE, monotonic=Mono(), quotes=q)
    return TestClient(app), sigs


def test_percentage_levels_are_resolved_from_the_live_ask():
    c, sigs = pct_rig()
    r = c.post("/webhook", content=json.dumps(PCT), headers=H)
    assert r.status_code == 200
    assert sigs[0].stop_loss == pytest.approx(75.2) and sigs[0].target == pytest.approx(140.35)   # ask 100.25


def test_percentage_and_absolute_may_be_mixed_across_levels_but_not_within_one():
    c, sigs = pct_rig()
    mixed = PCT | {"target_pct": None} | {"target": 150.0}
    mixed = {k: v for k, v in mixed.items() if v is not None}
    assert c.post("/webhook", content=json.dumps(mixed), headers=H).status_code == 200
    assert sigs[0].target == 150.0 and sigs[0].stop_loss == pytest.approx(75.2)
    both = PCT | {"stop_loss": 70.0, "idempotency_id": "tv-both-0001"}
    r = c.post("/webhook", content=json.dumps(both), headers=H)
    assert r.status_code == 422 and "not both" in json.dumps(r.json())


@pytest.mark.parametrize("changes", [{"stop_loss_pct": 0}, {"stop_loss_pct": 100}, {"stop_loss_pct": -5},
                                     {"stop_loss_pct": "25"}, {"target_pct": 0}, {"target_pct": 1001}])
def test_percentage_bounds(changes):
    c, sigs = pct_rig()
    assert c.post("/webhook", content=json.dumps(PCT | changes), headers=H).status_code == 422 and sigs == []


def test_percentage_levels_need_a_live_quote(rig):
    r = rig.post(PCT)                                              # this rig has no quote source
    assert r.status_code == 422 and "no live quote" in r.json()["detail"] and rig.signals == []
    c, sigs = pct_rig()
    other = PCT | {"strike": 25050, "idempotency_id": "tv-noquote-01"}
    assert c.post("/webhook", content=json.dumps(other), headers=H).status_code == 422


def test_percentage_levels_not_allowed_on_exit():
    c, sigs = pct_rig()
    r = c.post("/webhook", content=json.dumps(PCT | {"action": "EXIT"}), headers=H)
    assert r.status_code == 422 and sigs == []


def test_percentage_stop_through_the_real_pipeline(tmp_path):
    w = World(tmp_path, px={TOK: (100.0, 0.5)})
    c = TestClient(create_app(settings(), MASTER, w.mgr.submit, WebhookConfig(), clock=w.now, quotes=w.quote))
    r = c.post("/webhook", content=json.dumps(PCT), headers=H).json()
    assert r["status"] == "approved"
    w.advance(1)
    (pos,) = w.portfolio.open_positions()
    assert pos.stop_loss == pytest.approx(75.2) and pos.target == pytest.approx(140.35)
    wide = PCT | {"idempotency_id": "tv-widestop-01", "stop_loss_pct": 60}   # loss ~3,900 > the Rs2,000 limit
    w2 = World(tmp_path / "b", px={TOK: (100.0, 0.5)})
    c2 = TestClient(create_app(settings(), MASTER, w2.mgr.submit, WebhookConfig(), clock=w2.now, quotes=w2.quote))
    rej = c2.post("/webhook", content=json.dumps(wide), headers=H).json()
    assert rej["status"] == "rejected" and rej["reasons"][0]["code"] == "MAX_LOSS_PER_TRADE"


def test_the_tradingview_example_in_the_docs_is_accepted_with_real_placeholder_values():
    """docs/webhook.md BUY example, after TradingView substitutes {{ticker}} and {{timenow}} (UTC, 'Z' suffix)."""
    c, sigs = pct_rig()
    alert = {"secret": SECRET, "idempotency_id": "buy-NIFTY-2026-11-10T05:00:00Z", "timestamp": "2026-11-10T05:00:00Z",
             "symbol": "NIFTY", "action": "BUY", "strike": 25000, "option_type": "CE", "qty": 65,
             "stop_loss_pct": 25, "target_pct": 40}
    r = c.post("/webhook", content=json.dumps(alert), headers={"Content-Type": "text/plain"})   # TradingView may send text/plain
    assert r.status_code == 200, r.text
    assert sigs[0].idempotency_key == "webhook:buy-NIFTY-2026-11-10T05:00:00Z"
    exit_alert = {"secret": SECRET, "idempotency_id": "exit-NIFTY-2026-11-10T05:00:00Z", "symbol": "NIFTY",
                  "action": "EXIT", "strike": 25000, "option_type": "CE", "qty": 65}
    assert c.post("/webhook", content=json.dumps(exit_alert)).status_code == 200


def test_cloudflare_client_ip_is_trusted_only_from_configured_peers():
    """In Docker the tunnel arrives via the gateway, not loopback; the operator opts in with trusted_proxy_peers=["*"]."""
    cf = {"CF-Connecting-IP": "203.0.113.9"}
    base = dict(auth_failures={"max_per_minute": 2, "block_minutes": 10}, rate_limit={"per_minute": 600, "burst": 100})
    for peers, trusted in ((["127.0.0.1"], False), (["*"], True), (["testclient"], True)):
        rig = Rig(cfg=WebhookConfig(trusted_proxy_peers=peers, **base))
        for _ in range(2):
            rig.post(headers={"X-Webhook-Secret": "bad", **cf})
        # Trusted: the failures were counted against 203.0.113.9, so a different claimed address is NOT blocked.
        # Untrusted: they were counted against the real peer, so everyone behind it is blocked.
        assert (rig.post(headers=H | {"CF-Connecting-IP": "198.51.100.1"}).status_code == 200) is trusted
