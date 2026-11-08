"""Offline end-to-end demo of the real pipeline, no broker or credentials needed:

    python -m trader.demo_paper

strategy -> risk engine -> execution manager -> PAPER broker -> portfolio

It replays a SYNTHETIC morning (flat, then a climb) with option prices from Black-76
at a fixed 14% IV. State files go in a temporary folder, so your real data/ is
untouched. This shows plumbing only; it says nothing about profitability.
"""
from __future__ import annotations

import logging
import math
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .brokers import PaperBroker
from .execution import ExecutionManager, load_execution_config
from .feed import HealthStatus, Instr, Tick
from .greeks import DEFAULT_RATE, IST, bs_price, time_to_expiry
from .analytics import format_summary
from .instruments import InstrumentMaster, ensure_master
from .journal import Journal
from .logging_utils import setup_logging
from .portfolio import Portfolio
from .risk import RiskEngine, RiskStateStore, load_risk_config
from .strategy import LiveContext, StrategyRunner, build_strategies, load_strategy_config


def main() -> None:
    setup_logging(logging.WARNING)
    master = InstrumentMaster.load(ensure_master(), {"NIFTY"})
    # Pretend it is the weekday BEFORE an expiry. That is deliberate: with NIFTY's lot of 65
    # an ATM option a week from expiry costs ~Rs200+, so one lot's loss at a 25% stop (~Rs3,500)
    # would breach risk.yaml's Rs2,000 per-trade limit and the engine would (rightly) say no.
    # Close to expiry the premium is ~Rs80-100 and the example fits the limits.
    today = date.today()
    expiry = day = None
    for e in master.expiries("NIFTY", today):
        d = e - timedelta(days=1)
        if d.weekday() < 5 and d >= today:
            expiry, day = e, d
            break
    if expiry is None:
        raise SystemExit("could not find a suitable demo day in the instrument master")
    print(f"Demo day {day:%a %d-%b-%Y}, option expiry {expiry:%d-%b-%Y}\n")
    fut_i = Instr("NFO", master.nearest_future("NIFTY", day).token)
    idx_i = Instr("NSE", master.index_token("NIFTY"))

    clock = {"t": datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST)}
    now_dt = lambda: clock["t"]
    now_ts = lambda: clock["t"].timestamp()
    quotes: dict[str, Tick] = {}
    get_quote = lambda token: quotes.get(token)

    tmp = Path(tempfile.mkdtemp(prefix="paper_demo_"))
    risk_cfg = load_risk_config().model_copy(update={"state_file": str(tmp / "risk.json")})
    ecfg = load_execution_config()
    portfolio = Portfolio(get_quote, ecfg.fees, tmp / "portfolio.json", now_dt)
    healthy = HealthStatus(True, True, 0.0, 0.0, "ok")
    risk = RiskEngine(risk_cfg, portfolio, get_quote, lambda: healthy, RiskStateStore(risk_cfg.state_file), clock=now_dt)
    broker = PaperBroker(get_quote, ecfg.paper, clock=now_ts)
    mgr = ExecutionManager(risk, broker, portfolio, get_quote, ecfg, clock=now_ts)
    risk.set_actions(mgr)
    journal = Journal(tmp / "journal.db")
    risk.add_listener(journal.on_decision)
    mgr.add_listener(journal.on_event)

    def show(ev: dict) -> None:
        when = clock["t"].strftime("%H:%M:%S")
        t = ev["type"]
        if t == "order_placed":
            print(f"{when}  order   {ev['side']:<4} {ev['lots']}x {ev['symbol']} limit {ev['limit_price']}")
        elif t == "fill":
            print(f"{when}  FILL    {ev['side']:<4} {ev['lots']}x {ev['symbol']} @ {ev['price']}  (fee {ev['fee']})")
        elif t == "position_closed":
            print(f"{when}  CLOSED  {ev['position_id']}  realised P&L {ev['realised_pnl']:+,.0f} (after fees {ev['fees']:,.0f})")
        elif t == "warning":
            print(f"{when}  WARNING {ev['message']}")
    mgr.add_listener(show)
    risk.add_listener(lambda s, d: print(f"{clock['t']:%H:%M:%S}  RISK    {s.kind} {'approved' if d.approved else 'REJECTED: ' + d.summary()}")
                      if s.source != "system" else None)

    ctx = LiveContext(master, "NIFTY", expiry, fut_i, idx_i, portfolio)
    runner = StrategyRunner(build_strategies(load_strategy_config()), ctx, mgr.submit)

    end = clock["t"].replace(hour=10, minute=45)
    while clock["t"] < end:
        mins = (clock["t"] - clock["t"].replace(hour=9, minute=30)).total_seconds() / 60
        spot = 25000 + 2 * math.sin(mins) + 8 * max(0.0, min(mins - 15, 15))
        fut = spot + 30
        T = time_to_expiry(clock["t"], expiry)
        ts = now_ts()
        batch = [(idx_i, spot)]
        for o in master.chain("NIFTY", expiry, spot, 5):
            px = max(0.05, bs_price(fut, o.strike, T, DEFAULT_RATE, 0.14, o.opt_type, b=0.0))
            batch.append((Instr("NFO", o.token), px))
        batch.append((fut_i, fut))  # future last, as in the tests
        for ins, px in batch:
            tick = Tick(ins.exchange, ins.token, px, px - 0.25, px + 0.25, None, None, ts, ts)
            quotes[ins.token] = tick
            runner.on_tick(tick)
        mgr.poll(); risk.poll()
        clock["t"] += timedelta(seconds=5)

    print("\n--- end of demo ---")
    print(f"open positions : {len(portfolio.open_positions())}")
    print(f"realised P&L   : {portfolio.realised_pnl_today():+,.0f}   unrealised: {portfolio.unrealised_pnl():+,.0f}")
    print(f"(state files were in {tmp})\n")
    print(format_summary(journal.summary(day, day)))
    print(f"\nTry the real tools on this demo journal:\n  python -m trader.journal --journal {tmp}/journal.db trades --from {day} --to {day}"
          f"\n  python -m trader.journal --journal {tmp}/journal.db review --id 1 --followed y --notes 'as planned'")


if __name__ == "__main__":
    main()
