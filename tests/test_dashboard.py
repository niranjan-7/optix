import json
import math
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.exec_helpers import EXP, HEALTHY, TUE, World, leg
from tests.test_instruments import sample_rows
from tests.test_risk import SICK
from tests.test_strategy import INDEX_ROW
from tests.test_webhook import settings
from trader.dashboard import STATIC, DashboardData, hostname, install_dashboard
from trader.feed import Instr, Tick
from trader.greeks import DEFAULT_RATE, IST, bs_price, time_to_expiry
from trader.instruments import InstrumentMaster
from trader.journal import Journal
from trader.webhook import WebhookConfig, create_app

MASTER = InstrumentMaster.from_rows(sample_rows() + [INDEX_ROW], {"NIFTY"})
IDX, FUT = Instr("NSE", "99926000"), Instr("NFO", "58875")
SPOT, FUTURE, IV = 25001.0, 25030.0, 0.14


def priced(sigma=IV, half=0.25):
    """Black-76 prices for every strike at one known volatility, keyed by master token."""
    T = time_to_expiry(TUE, EXP)
    px = {}
    for o in MASTER.chain("NIFTY", EXP, SPOT, 4):
        px[o.token] = (bs_price(FUTURE, o.strike, T, DEFAULT_RATE, sigma, o.opt_type, b=0.0), half * 2)
    return px


class Rig:
    def __init__(self, tmp_path, health=HEALTHY, world_kw=None, **kw):
        self.w = World(tmp_path / "w", px=priced(), **(world_kw or {}))
        self.health = health
        self.j = Journal(tmp_path / "j.db")
        self.w.risk.add_listener(self.j.on_decision)
        self.w.mgr.add_listener(self.j.on_event)
        self.overrides: dict = {}
        self.kills: list[str] = []
        self.data = DashboardData(
            underlying="NIFTY", expiry=EXP, master=MASTER, latest=self.latest, index=IDX, future=FUT,
            portfolio=self.w.portfolio, risk=self.w.risk, feed_health=lambda: self.health, strikes_each_side=2,
            journal=self.j, active_executions=lambda: self.w.mgr.active_count, clock=self.w.now, **kw)

    def latest(self, ins):
        if ins in self.overrides:
            return self.overrides[ins]
        if ins == IDX:
            return Tick("NSE", ins.token, SPOT, None, None, None, None, None, self.w.t)
        if ins == FUT:
            return Tick("NFO", ins.token, FUTURE, FUTURE - 0.25, FUTURE + 0.25, None, None, None, self.w.t)
        return self.w.quote(ins.token)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def by_strike(state):
    return {r["strike"]: r for r in state["chain"]}


# =============================================================== the data

def test_chain_is_centred_on_the_money_with_iv_recovered_from_prices(rig):
    s = rig.data.state()
    assert [r["strike"] for r in s["chain"]] == [24900, 24950, 25000, 25050, 25100]
    assert [r["strike"] for r in s["chain"] if r["atm"]] == [25000]
    assert s["market"]["atm"] == 25000 and s["market"]["lot_size"] == 65 and s["market"]["days_to_expiry"] == 7
    for r in s["chain"]:
        for side, sign in (("ce", 1), ("pe", -1)):
            leg = r[side]
            assert leg["iv"] == pytest.approx(IV * 100, abs=0.05)          # prices were made at exactly 14%
            assert sign * leg["delta"] > 0 and abs(leg["delta"]) < 1
            assert leg["gamma"] > 0 and leg["vega"] > 0 and leg["theta"] < 0
            assert leg["stale"] is False and leg["bid"] < leg["ltp"] < leg["ask"]
    atm = by_strike(s)[25000]
    assert 0.45 < atm["ce"]["delta"] < 0.6 and -0.55 < atm["pe"]["delta"] < -0.4
    assert atm["ce"]["symbol"] == "NIFTY17NOV2625000CE"
    assert (s["market"]["spot"], s["market"]["future"]) == (SPOT, FUTURE)


def test_stale_and_missing_quotes_degrade_gracefully(rig):
    old = rig.w.quote("25000CE17")
    rig.overrides[Instr("NFO", "25000CE17")] = Tick("NFO", "25000CE17", old.ltp, old.bid, old.ask, None, None, None, rig.w.t - 120)
    rig.overrides[Instr("NFO", "25000PE17")] = None
    s = by_strike(rig.data.state())[25000]
    assert s["ce"]["stale"] is True and s["ce"]["age"] == 120 and s["ce"]["iv"] is not None
    assert s["pe"]["ltp"] is None and s["pe"]["iv"] is None and s["pe"]["stale"] is True


def test_no_market_data_at_all_gives_an_empty_chain_not_an_error(rig):
    rig.overrides[IDX] = None
    rig.overrides[FUT] = None
    s = rig.data.state()
    assert s["chain"] == [] and s["market"]["spot"] is None and s["market"]["atm"] is None


def test_future_is_the_fallback_reference_when_spot_is_missing(rig):
    rig.overrides[IDX] = None
    assert rig.data.state()["market"]["atm"] == 25050                      # 25030 is nearer 25050 than 25000


def test_nan_and_infinity_become_null_so_the_json_is_valid(rig):
    rig.overrides[IDX] = Tick("NSE", IDX.token, float("nan"), None, None, None, None, None, rig.w.t)
    rig.overrides[Instr("NFO", "25000CE17")] = Tick("NFO", "25000CE17", float("inf"), float("nan"), 1.0, None, None, None, rig.w.t)
    s = rig.data.state()
    json.dumps(s, allow_nan=False)                                          # would raise on any NaN/Infinity
    assert s["market"]["spot"] is None


def test_positions_show_entry_mark_and_open_pnl(rig):
    w = rig.w
    assert w.mgr.submit(w.entry("e1")).approved
    w.advance(1)
    s = rig.data.state()
    (p,) = s["positions"]
    (leg,) = p["legs"]
    assert (p["position_id"], p["strategy"], p["stop_loss"], p["target"]) == ("P1", "ex", 75.2, 140.0)
    assert leg["side"] == "BUY" and leg["lots"] == 1 and leg["entry"] == pytest.approx(100.30)
    bid = w.quote("25000CE").bid
    assert leg["mark"] == pytest.approx(bid)                                # longs are valued at the bid
    assert leg["pnl"] == pytest.approx((leg["mark"] - leg["entry"]) * 65)
    assert s["pnl"]["unrealised"] == pytest.approx(p["unrealised"]) == pytest.approx(w.portfolio.unrealised_pnl())
    assert s["pnl"]["realised"] == pytest.approx(-20.0)                     # the entry brokerage
    assert s["pnl"]["total"] == pytest.approx(s["pnl"]["realised"] + s["pnl"]["unrealised"])


def test_risk_panel_reports_limits_and_how_much_is_used(rig):
    s0 = rig.data.state()["risk"]
    assert (s0["loss_limit"], s0["max_loss_per_trade"], s0["orders_limit"], s0["positions_limit"]) == (4000, 2000, 5, 2)
    assert s0["allowed_structures"] == ["LONG_OPTION"] and s0["entry_window"] == "09:20-15:00" and s0["force_square_off"] == "15:20"
    assert s0["window_open"] is True and s0["kill_active"] is False and s0["loss_used"] == 0
    rig.w.mgr.submit(rig.w.entry("e1")); rig.w.advance(1)
    s1 = rig.data.state()["risk"]
    assert s1["orders_used"] == 1 and s1["positions_open"] == 1


def test_loss_used_counts_only_losses(rig):
    rig.w.portfolio.record_entry_fill(None, "ex", None, None, leg(), 1, 100.0, True)
    s = rig.data.state()
    assert s["risk"]["loss_used"] == pytest.approx(max(0.0, -s["pnl"]["total"])) and s["risk"]["loss_used"] > 0   # fee + spread


def test_window_flag_follows_the_clock(rig):
    assert rig.data.state()["risk"]["window_open"] is True
    rig.w.t = datetime(2026, 11, 10, 8, 0, tzinfo=IST).timestamp()
    assert rig.data.state()["risk"]["window_open"] is False
    rig.w.t = datetime(2026, 11, 14, 11, 0, tzinfo=IST).timestamp()           # Saturday
    assert rig.data.state()["risk"]["window_open"] is False


def test_feed_and_kill_state_are_shown(rig):
    rig.health = SICK
    s = rig.data.state()
    assert s["feed"]["healthy"] is False and "not connected" in s["feed"]["reason"]
    rig.w.risk.kill("testing the panel")
    r = rig.data.state()["risk"]
    assert r["kill_active"] and r["kill_reason"] == "testing the panel"


def test_recent_decisions_come_from_the_journal_newest_first(rig):
    rig.w.mgr.submit(rig.w.entry("ok1")); rig.w.mgr.submit(rig.w.entry("bad1", sl=None))
    d = rig.data.state()["recent_decisions"]
    assert [x["approved"] for x in d] == [False, True] and d[0]["codes"] == ["NO_STOP_LOSS"] and "stop-loss" in d[0]["detail"]


def test_mode_and_source_labels(tmp_path):
    assert Rig(tmp_path).data.state()["mode"] == "PAPER"
    live = Rig(tmp_path / "x", live=True, data_source="angel")
    s = live.data.state()
    assert s["mode"] == "LIVE" and s["data_source"] == "angel"


# ============================================================== positions_detail

def test_positions_detail_marks_shorts_at_the_ask(tmp_path):
    from tests.test_portfolio import Env
    from tests.test_risk import leg
    e = Env()
    pid, _ = e.pf.record_entry_fill(None, "s", 80.0, 10.0, leg("SELL", 25000), 1, 110.0, True)
    e.pf.record_entry_fill(pid, "s", 80.0, 10.0, leg("BUY", 25100), 1, 60.0, True)
    e.q = {"25000CE": (108.0, 108.5), "25100CE": (62.0, 62.5)}
    (d,) = e.pf.positions_detail()
    legs = {l["side"]: l for l in d["legs"]}
    assert legs["SELL"]["mark"] == 108.5 and legs["SELL"]["pnl"] == pytest.approx(1.5 * 65)
    assert legs["BUY"]["mark"] == 62.0 and legs["BUY"]["pnl"] == pytest.approx(2.0 * 65)
    assert d["unrealised"] == pytest.approx(3.5 * 65) == pytest.approx(e.pf.unrealised_pnl())
    e.pf.record_exit_fill(pid, leg("BUY", 25000), 1, 100.0, True); e.pf.record_exit_fill(pid, leg("SELL", 25100), 1, 70.0, True)
    assert e.pf.positions_detail() == []


# ================================================================= HTTP layer

def app_for(rig):
    app = create_app(settings(), MASTER, rig.w.mgr.submit, WebhookConfig(), clock=rig.w.now, quotes=rig.w.quote)
    install_dashboard(app, rig.data, lambda reason: (rig.kills.append(reason), rig.w.risk.kill(reason, "dashboard")))
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def client(rig):
    return app_for(rig)


KILL_HEADERS = {"X-Requested-With": "dashboard"}


def test_page_and_assets_are_served_with_a_strict_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "KILL SWITCH" in r.text and "/static/dashboard.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "javascript" in client.get("/static/dashboard.js").headers["content-type"]
    assert "text/css" in client.get("/static/dashboard.css").headers["content-type"]
    assert client.get("/static/../src/trader/risk.py").status_code == 404


def test_state_endpoint_returns_valid_json(client):
    r = client.get("/api/state")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert {"feed", "market", "chain", "positions", "pnl", "risk", "recent_decisions", "mode"} <= set(s)


def test_page_makes_no_external_requests_and_never_uses_innerhtml():
    html = (STATIC / "dashboard.html").read_text()
    js = (STATIC / "dashboard.js").read_text()
    css = (STATIC / "dashboard.css").read_text()
    for name, text in (("html", html), ("js", js), ("css", css)):
        assert not re.search(r"https?://", text), f"{name} references an external address"
        assert "@import" not in text and "url(" not in text
    code = re.sub(r"//[^\n]*", "", js)                                   # judge the code, not the comments
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in code, banned
    assert "style=" not in html and "<script>" not in html and "onclick" not in html.lower()   # compatible with the strict CSP


def test_foreign_host_headers_are_refused_dns_rebinding(client):
    for path in ("/", "/api/state", "/static/dashboard.js"):
        for host in ("evil.example", "localhost.evil.example", "127.0.0.1.evil.example", "192.168.1.5:8000", ""):
            assert client.get(path, headers={"Host": host}).status_code == 403, (path, host)
    assert client.post("/api/kill", headers={**KILL_HEADERS, "Host": "evil.example"}).status_code == 403
    for ok in ("localhost", "LOCALHOST:8000", "127.0.0.1:8000", "[::1]:8000"):
        assert client.get("/api/state", headers={"Host": ok}).status_code == 200, ok
    assert client.get("/health", headers={"Host": "anything.example"}).status_code == 200      # public path unaffected


def test_hostname_parsing():
    assert [hostname(h) for h in ("localhost", "localhost:8000", "127.0.0.1:80", "[::1]:8000", "[::1]", "EXAMPLE.com:1")] == [
        "localhost", "localhost", "127.0.0.1", "::1", "::1", "example.com"]


def test_tunnel_traffic_cannot_reach_the_dashboard_or_kill(client, rig):
    tunnel = {"CF-Connecting-IP": "203.0.113.5", "CF-Ray": "abc123"}
    for path in ("/", "/api/state", "/static/dashboard.js"):
        assert client.get(path, headers=tunnel).status_code == 404
    assert client.post("/api/kill", headers={**KILL_HEADERS, **tunnel}).status_code == 404
    assert rig.kills == [] and not rig.w.risk.status()["kill_active"]
    assert client.get("/health", headers=tunnel).status_code == 200


# --------------------------------------------------------------- the kill button

def test_kill_requires_the_dashboards_own_header(client, rig):
    assert client.post("/api/kill").status_code == 403                                    # e.g. a plain cross-site form post
    assert client.post("/api/kill", headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 403
    assert client.get("/api/kill").status_code == 405
    assert rig.kills == []


@pytest.mark.parametrize("extra", [{"Origin": "http://evil.example"}, {"Origin": "https://localhost.evil.example"},
                                   {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}])
def test_kill_refuses_cross_site_requests_even_with_the_header(client, rig, extra):
    assert client.post("/api/kill", headers={**KILL_HEADERS, **extra}).status_code == 403
    assert rig.kills == [] and not rig.w.risk.status()["kill_active"]


def test_kill_from_the_page_stops_everything_and_closes_positions(client, rig):
    w = rig.w
    w.mgr.submit(w.entry("e1")); w.advance(1)
    assert len(w.portfolio.open_positions()) == 1
    r = client.post("/api/kill", headers={**KILL_HEADERS, "Origin": "http://localhost:8000", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and r.json() == {"status": "kill_switch_on"}
    assert rig.kills == ["dashboard KILL button"]
    w.advance(2)
    assert w.portfolio.open_positions() == []
    s = client.get("/api/state").json()
    assert s["risk"]["kill_active"] and s["risk"]["kill_source"] == "dashboard"
    assert client.post("/api/kill", headers=KILL_HEADERS).status_code == 200            # pressing again is harmless
    assert w.mgr.submit(w.entry("again")).approved is False                              # and entries stay blocked


def test_kill_failure_is_reported_honestly(rig):
    app = create_app(settings(), MASTER, rig.w.mgr.submit, WebhookConfig(), clock=rig.w.now)
    def boom(reason): raise RuntimeError("broker down")
    install_dashboard(app, rig.data, boom)
    r = TestClient(app, base_url="http://localhost").post("/api/kill", headers=KILL_HEADERS)
    assert r.status_code == 500 and "check the kill switch state" in r.json()["detail"] and "broker down" not in r.text


def test_state_failure_returns_500_not_a_stack_trace(rig):
    rig.data.state = lambda: (_ for _ in ()).throw(RuntimeError("secret internals"))
    r = app_for(rig).get("/api/state")
    assert r.status_code == 500 and "secret internals" not in r.text


def test_there_is_no_reset_endpoint_on_purpose(client):
    for path in ("/api/reset", "/api/reset-kill", "/reset"):
        assert client.post(path, headers=KILL_HEADERS).status_code in (404, 405)
    js = (STATIC / "dashboard.js").read_text()
    assert set(re.findall(r"fetch\(\s*\"([^\"]+)\"", js)) == {"/api/state", "/api/kill"}   # the page can call nothing else


def test_a_profitable_day_uses_none_of_the_loss_limit(rig):
    """Profit must never show as 'loss used' (a gap an abs() instead of max(0, -x) would open)."""
    rig.w.portfolio.record_entry_fill(None, "ex", None, None, leg(), 1, 90.0, True)     # bought at 90; market bid is 99.75
    s = rig.data.state()
    assert s["pnl"]["total"] > 500 and s["risk"]["loss_used"] == 0
