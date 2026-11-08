"""Assembles every module into one running system.

    python -m trader                      # PAPER trading on REAL Angel One prices (the normal mode)
    python -m trader --feed simulated     # try it with no Angel One account, on made-up prices

Modes (rule 1: paper is the default, and nothing here can turn live trading on by itself):
  * PAPER + live prices (default): real market data, orders go to the paper broker. This is what the
    4-week paper phase should use.
  * PAPER + simulated prices: for learning the system and for development.
  * LIVE orders: only if LIVE_TRADING=true in .env AND a person types the confirmation phrase at start-up
    (see brokers.confirm_live_trading) AND the prices are real. A restart loop in Docker cannot satisfy
    the confirmation, so live mode can never resume unattended.
"""
from __future__ import annotations

import argparse
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .alerts import Alerts, attach_commands, build_alerts, load_alerts_config
from .auth import AngelAuth, AuthError
from .brokers import BrokerAdapter, LiveTradingNotConfirmed, build_broker
from .config import ConfigError, Settings, load_settings, require_webhook_secret
from .dashboard import DashboardData, install_dashboard
from .execution import ExecutionManager, load_execution_config
from .feed import AngelFeed, ChainTracker, Feed, FeedHealth, Instr, SimulatedFeed, Tick, load_feed_config
from .greeks import DEFAULT_RATE, IST, bs_price, time_to_expiry
from .instruments import InstrumentMaster, ensure_master
from .journal import Journal, load_journal_config
from .logging_utils import setup_logging
from .ops import DailyRestart, Heartbeat, OpsLoop, TokenKeeper, load_ops_config
from .portfolio import Portfolio
from .risk import RiskEngine, RiskStateStore, load_risk_config
from .snapshots import SnapshotRecorder, SnapshotStore
from .strategy import LiveContext, StrategyRunner, build_strategies, load_strategy_config
from .webhook import create_app, load_webhook_config

log = logging.getLogger(__name__)


@dataclass
class System:
    mode: str  # "PAPER" | "LIVE" (real orders)
    data_source: str  # "simulated" | "angel"
    master: InstrumentMaster
    feed: Feed
    ctx: LiveContext
    portfolio: Portfolio
    risk: RiskEngine
    mgr: ExecutionManager
    runner: StrategyRunner
    journal: Journal
    alerts: Alerts
    tracker: ChainTracker
    broker: BrokerAdapter
    expiry: date
    stop: Callable[[], None]
    restart_event: threading.Event = field(default_factory=threading.Event)
    auth: Any = None
    prices: dict[Instr, float] | None = None  # starting prices (simulated feed only)
    ops: Any = None  # OpsLoop: heartbeat, daily restart, login upkeep

    @property
    def live(self) -> bool:
        return self.mode == "LIVE"


def build_system(
    settings: Settings,
    *,
    data_dir: str | Path = "data",
    feed_mode: str = "live",
    today: date | None = None,
    clock: Callable[[], datetime] | None = None,
    auth_factory: Callable[[Settings], Any] = AngelAuth,
    socket_factory: Callable[[Any, Any], Any] | None = None,
    input_fn: Callable[[str], str] | None = None,
    spot: float = 25000.0,
    start: bool = True,
) -> System:
    if feed_mode not in ("live", "simulated"):
        raise SystemExit(f"unknown feed mode {feed_mode!r}")
    if settings.live_trading and feed_mode != "live":
        # Real money on made-up prices would be absurd; refuse before anything else happens.
        raise SystemExit("LIVE_TRADING=true but the prices are SIMULATED. Real orders need real prices "
                         "(--feed live). Refusing to start.")

    if feed_mode == "live":  # fail fast, before downloading anything
        try:
            settings.require_angel_credentials()
        except ConfigError as e:
            raise SystemExit(f"{e}\n(To try the system without an Angel One account, start it with:  python -m trader --feed simulated)")

    data_dir = Path(data_dir)
    today = today or date.today()
    clock = clock or (lambda: datetime.now(IST))
    fcfg, wcfg = load_feed_config(), load_webhook_config()
    master = InstrumentMaster.load(ensure_master(data_dir / "instruments", today), {fcfg.underlying})
    expiry = master.nearest_expiry(fcfg.underlying, today, skip_today=wcfg.skip_expiry_day)

    # ---- market data
    auth: Any = None
    prices: dict[Instr, float] | None = None
    if feed_mode == "simulated":
        fut = Instr("NFO", master.nearest_future(fcfg.underlying, today).token)
        idx = Instr("NSE", master.index_token(fcfg.underlying))
        prices = {idx: spot, fut: spot + 30}  # Black-76 at a flat 14% IV: a placeholder market, not reality
        T = time_to_expiry(clock(), expiry)
        for o in master.chain(fcfg.underlying, expiry, spot, 15):
            prices[Instr("NFO", o.token)] = max(0.1, bs_price(spot + 30, o.strike, max(T, 1e-4), DEFAULT_RATE, 0.14, o.opt_type, b=0.0))
        feed: Feed = SimulatedFeed(prices, health=FeedHealth(fcfg.stale_seconds), seed=7, tick_interval=0.5, step_vol=0.0003)
        source = "simulated"
    else:
        auth = auth_factory(settings)
        try:
            auth.login()  # fail now, loudly, rather than discover a bad login later
        except AuthError as e:
            raise SystemExit(f"Angel One login failed: {e}")
        feed = AngelFeed(auth, settings, fcfg, socket_factory=socket_factory) if socket_factory else AngelFeed(auth, settings, fcfg)
        source = "angel"
    tracker = ChainTracker(feed, master, fcfg.underlying, expiry, fcfg.strikes_each_side, today)

    ctx_holder: dict = {}
    quote: Callable[[str], Tick | None] = lambda token: ctx_holder["ctx"].latest(Instr("NFO", token))

    # ---- risk, portfolio, execution
    risk_cfg = load_risk_config().model_copy(update={"state_file": str(data_dir / "risk_state.json")})
    ecfg = load_execution_config()
    portfolio = Portfolio(quote, ecfg.fees, data_dir / "portfolio.json", clock)
    ctx = LiveContext(master, fcfg.underlying, expiry, tracker.future, tracker.index, portfolio)
    ctx_holder["ctx"] = ctx
    risk = RiskEngine(risk_cfg, portfolio, quote, feed.health.status, RiskStateStore(risk_cfg.state_file), clock=clock)
    try:
        # Paper broker unless LIVE_TRADING=true AND a person types the confirmation phrase.
        broker = build_broker(settings, auth, quote, ecfg.paper, ecfg.live.product_type, input_fn)
    except LiveTradingNotConfirmed as e:
        raise SystemExit(f"Live trading was NOT enabled: {e}")
    mode = "LIVE" if broker.name == "angel" else "PAPER"
    mgr = ExecutionManager(risk, broker, portfolio, quote, ecfg)
    risk.set_actions(mgr)
    runner = StrategyRunner(build_strategies(load_strategy_config()), ctx, mgr.submit)
    runner.attach(feed)

    # ---- record everything
    jcfg = load_journal_config()
    journal = Journal(data_dir / "journal.db")
    risk.add_listener(journal.on_decision)
    mgr.add_listener(journal.on_event)
    if jcfg.snapshots.enabled:
        store = SnapshotStore(data_dir / "market.db")
        store.prune(jcfg.snapshots.keep_days)
        feed.on_tick(SnapshotRecorder(store, fcfg.underlying, expiry, tracker.index, tracker.future,
                                      jcfg.snapshots.interval_seconds,
                                      describe=lambda ins: master.describe(ins.exchange, ins.token)).on_tick)

    # ---- alerts and phone commands (a harmless no-op without Telegram credentials)
    acfg = load_alerts_config().model_copy(update={"state_file": str(data_dir / "alerts_state.json")})
    alerts, tg_api, tg_chat = build_alerts(settings, acfg, live=(mode == "LIVE"))
    risk.set_notifier(alerts.risk_notice)
    risk.add_listener(alerts.on_decision)
    mgr.add_listener(alerts.on_event)
    alerts.bind(health=feed.health.status, open_positions=lambda: len(portfolio.open_positions()),
                weekdays=tuple(risk_cfg.window.weekdays), journal=journal, portfolio=portfolio, risk=risk)
    commands = attach_commands(alerts, tg_api, tg_chat, risk=risk, portfolio=portfolio,
                               health=feed.health.status) if tg_api and tg_chat else None

    # ---- operations: heartbeat, daily restart, login upkeep
    ocfg = load_ops_config()
    restart_event = threading.Event()
    heartbeat = Heartbeat(settings.heartbeat_url.get_secret_value(), ocfg.heartbeat_seconds)
    ops = OpsLoop([
        heartbeat if heartbeat.enabled else None,
        TokenKeeper(auth, ocfg.token_check_seconds, alerts.send) if auth is not None else None,
        DailyRestart(ocfg.daily_restart, clock(), clock, lambda: bool(portfolio.open_positions()) or mgr.active_count > 0,
                     restart_event.set, alerts.send, ocfg.restart_postponed_alert_seconds),
    ])

    stop_evt = threading.Event()

    def scheduler() -> None:
        while not stop_evt.wait(1.0):
            for fn in (mgr.poll, risk.poll, alerts.tick, ops.tick):
                try:
                    fn()
                except Exception:
                    log.exception("scheduler task %s failed", getattr(fn, "__name__", fn))

    def stop() -> None:
        alerts.send("INFO", "⏹️ System stopping.")
        stop_evt.set()
        feed.stop()
        alerts.stop()

    system = System(mode, source, master, feed, ctx, portfolio, risk, mgr, runner, journal, alerts, tracker, broker,
                    expiry, stop, restart_event, auth, prices, ops)
    if start:
        threading.Thread(target=scheduler, name="scheduler", daemon=True).start()
        problems = mgr.recover()  # cancel leftover broker orders; compare positions with the broker
        alerts.start()
        if commands:
            commands.start()
        extra = " REAL MONEY." if mode == "LIVE" else ""
        alerts.send("INFO", f"▶️ System started ({mode} mode, {source} prices).{extra} Send /help for commands.")
        if problems:
            alerts.send("CRITICAL", "Positions do not match the broker at start-up: " + "; ".join(problems))
        feed.start()
    return system


def banner(system: System, host: str, port: int) -> str:
    if system.live:
        head = ("!" * 70 + "\n  LIVE TRADING: REAL ORDERS WITH REAL MONEY.\n  Kill switch: dashboard button, Telegram /kill, or  "
                "python -m trader.risk kill\n" + "!" * 70)
    else:
        head = ("PAPER MODE: no real orders are sent. Prices are "
                + ("REAL (Angel One)." if system.data_source == "angel" else "SIMULATED (made up)."))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # 0.0.0.0 (Docker) is not an address you can browse to
    return f"\n{head}\nDashboard: http://{shown}:{port}/   (open in your browser; this computer only)\nWebhook:   http://{shown}:{port}/webhook"


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m trader", description="Semi-automated NIFTY options system (paper mode by default)")
    ap.add_argument("--data-dir", default="data", help="where state, the journal and recorded prices are kept")
    ap.add_argument("--host", help="address to listen on (default from webhook.yaml: this computer only). "
                                   "Inside Docker use 0.0.0.0 together with a localhost-only published port.")
    ap.add_argument("--trust-proxy-from-any-peer", action="store_true",
                    help="trust Cloudflare's client-IP header from any direct peer. ONLY safe when the port is reachable "
                         "from this computer alone, as docker-compose.yml arranges.")
    ap.add_argument("--feed", choices=["live", "simulated"], default=None,
                    help="live = real Angel One prices (needs credentials); simulated = made-up prices for trying it out. "
                         "Default: TRADER_FEED from .env, else live")
    args = ap.parse_args(argv)

    setup_logging(logging.INFO)
    settings = load_settings()
    try:
        require_webhook_secret(settings)
    except ConfigError as e:
        raise SystemExit(f"Cannot start: {e}")

    system = build_system(settings, data_dir=args.data_dir, feed_mode=args.feed or settings.trader_feed)
    wcfg, fcfg = load_webhook_config(), load_feed_config()
    if args.host:
        wcfg = wcfg.model_copy(update={"host": args.host})
    if args.trust_proxy_from_any_peer:
        wcfg = wcfg.model_copy(update={"trusted_proxy_peers": ["*"]})
    app = create_app(settings, system.master, system.mgr.submit, wcfg,
                     quotes=lambda tok: system.ctx.latest(Instr("NFO", tok)))
    install_dashboard(
        app,
        DashboardData(underlying=fcfg.underlying, expiry=system.expiry, master=system.master, latest=system.ctx.latest,
                      index=system.tracker.index, future=system.tracker.future, portfolio=system.portfolio,
                      risk=system.risk, feed_health=system.feed.health.status, strikes_each_side=fcfg.strikes_each_side,
                      journal=system.journal, live=system.live, data_source=system.data_source,
                      active_executions=lambda: system.mgr.active_count),
        lambda reason: system.risk.kill(reason, "dashboard"))
    print(banner(system, wcfg.host, wcfg.port))

    if system.prices:  # a ready-to-paste test, only meaningful on simulated prices
        best = min((o for o in system.master.chain(fcfg.underlying, system.expiry, 25000.0, 15) if o.opt_type == "CE"),
                   key=lambda o: abs(system.prices[Instr("NFO", o.token)] - 90))
        print("\nTry the webhook (replace YOUR_SECRET with WEBHOOK_SECRET from .env):\n")
        print(f"""curl -s -X POST http://{wcfg.host}:{wcfg.port}/webhook -H "Content-Type: application/json" \\
  -H "X-Webhook-Secret: YOUR_SECRET" -d '{{"idempotency_id":"test-{datetime.now():%H%M%S}","symbol":"NIFTY","action":"BUY","strike":{best.strike:g},"option_type":"CE","qty":{best.lot_size},"stop_loss_pct":25,"target_pct":40}}'
""")

    server = uvicorn.Server(uvicorn.Config(app, host=wcfg.host, port=wcfg.port, log_level="warning", server_header=False))
    threading.Thread(target=lambda: (system.restart_event.wait(), setattr(server, "should_exit", True)),
                     name="restart-watcher", daemon=True).start()
    try:
        server.run()  # returns on Ctrl-C, "docker stop", or the daily restart
    finally:
        system.stop()
    if system.restart_event.is_set():
        log.info("Daily restart: exiting so the supervisor (Docker) starts a fresh process.")
