"""Backtest STUB: replay recorded chain snapshots through the real strategy + risk engine.

It deliberately reuses the production pieces (strategy runner, risk engine, execution manager,
paper broker, portfolio, journal) with a simulated clock, so a backtest exercises the SAME rules
as paper trading. Nothing here can place a real order: only the paper broker is ever created,
and all state lives in a throw-away work folder, never in your real data/.

    python -m trader.backtest run --from 2026-11-02 --to 2026-11-13 --split 2026-11-09

READ THIS BEFORE TRUSTING ANY NUMBER (the CLI prints it too):

1. SAMPLED DATA. Snapshots are taken every few seconds, not every tick. Entries happen later than
   live, a stop-loss or target can be overshot or missed between samples, and orders are only
   checked for fills at snapshot times (up to one interval later than live).
2. NO REAL SLIPPAGE. Fills use the paper model (touch price +/- 1 tick): no queue position, no
   market impact, no partial fills, no outages. Real fills in a fast market are worse.
3. FEES ARE AN ESTIMATE (brokerage only unless you set more in execution.yaml).
4. OVERFITTING. If you change rules until the backtest looks good, you are fitting noise. A few days
   prove nothing. Change ONE thing at a time, keep every variant in git, and judge a change on days
   you did NOT use to design it (--split reports before/after separately).
5. YOUR RECORDING IS THE LIMIT. Gaps in the recording are gaps in the test. A contract that was
   outside the recorded strike window at the time cannot be traded in the replay.
6. IT USES TODAY'S CODE AND LIMITS, not the ones in force on the day.
7. A good backtest is a reason to keep paper trading, never a reason to go live.
"""
from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .analytics import Summary, format_summary
from .brokers import PaperBroker
from .execution import ExecutionConfig, ExecutionManager, load_execution_config
from .feed import FeedHealth, Instr, Tick, load_feed_config
from .greeks import IST
from .instruments import InstrumentMaster
from .journal import Journal, TradeRow
from .portfolio import Portfolio
from .risk import RiskConfig, RiskEngine, RiskStateStore, load_risk_config
from .snapshots import Snapshot, SnapshotStore
from .strategy import LiveContext, StrategyRunner, build_strategies, load_strategy_config

log = logging.getLogger(__name__)

LIMITS_TEXT = """\
BACKTEST - READ THE LIMITS
  * Replays SAMPLED snapshots, not every tick: entries are later than live, and stops/targets can be
    overshot or missed between samples.
  * Fills are the paper model (touch price +/- 1 tick). No queue, no market impact, no real slippage.
  * Fees are an estimate. It uses today's code and today's limits.
  * OVERFITTING: tuning rules until this looks good fits noise. Change one thing at a time, keep each
    variant in git, and judge changes on days you did not use to design them (use --split).
  * A good backtest is a reason to keep paper trading, not a reason to go live."""

MIN_DAYS_FOR_ANY_CONFIDENCE = 20
MIN_TRADES_FOR_ANY_CONFIDENCE = 30


class BacktestError(RuntimeError):
    """Something prevents a meaningful replay (message is operator-facing)."""


# ================================================================ replay pieces

class SimClock:
    def __init__(self) -> None:
        self.t = 0.0

    def epoch(self) -> float:
        return self.t

    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.t, IST)


def snapshot_ticks(snap: Snapshot, prev_ts: float | None) -> list[Tick]:
    """Turn one snapshot back into ticks, in time order, exposing only what was known at snap.ts.

    A quote is re-emitted only if it UPDATED since the previous snapshot (its update time is
    snap.ts - age); otherwise the replay already has it, and its age keeps growing exactly as it
    would have live, so the risk engine's staleness checks behave the same. The spot index has no
    stored age, so it is treated as fresh at every snapshot.
    """
    ticks: list[Tick] = []
    for key, q in snap.quotes.items():
        exchange, token = key.split(":", 1)
        updated = snap.ts - q.age
        if prev_ts is not None and updated <= prev_ts + 0.001:  # ages are stored to the millisecond
            continue
        ticks.append(Tick(exchange, token, q.ltp, q.bid, q.ask, q.volume, q.oi, None, updated))
    if snap.spot is not None and snap.index_token:
        ticks.append(Tick("NSE", snap.index_token, snap.spot, None, None, None, None, None, snap.ts))
    # Oldest first; the future last among equals (the strategy reacts to future ticks, so the
    # option quotes it needs must already be in place - same convention as the live demos).
    ticks.sort(key=lambda t: (t.received_at, t.token == snap.future_token))
    return ticks


@dataclass
class DayQuality:
    day: str
    snapshots: int = 0
    first: float = 0.0
    last: float = 0.0
    max_gap: float = 0.0  # seconds between consecutive snapshots


@dataclass
class BacktestResult:
    start: date
    end: date
    snapshots: int
    days: list[DayQuality]
    summary: Summary
    parts: dict[str, Summary] = field(default_factory=dict)
    trades: list[TradeRow] = field(default_factory=list)
    open_positions: int = 0
    warnings: list[str] = field(default_factory=list)
    work_dir: Path | None = None
    journal: Journal | None = None


class Backtester:
    def __init__(self, store: SnapshotStore, work_dir: str | Path | None = None,
                 strategy_config_path: str | Path = "config/strategy.yaml",
                 risk_config: RiskConfig | None = None, execution_config: ExecutionConfig | None = None,
                 stale_seconds: float | None = None):
        self._store = store
        self._work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="backtest_"))
        self._strategy_path = strategy_config_path
        self._risk_cfg = risk_config or load_risk_config()
        self._exec_cfg = execution_config or load_execution_config()
        self._stale = stale_seconds if stale_seconds is not None else load_feed_config().stale_seconds

    def run(self, start: date, end: date, split: date | None = None) -> BacktestResult:
        if end < start:
            raise BacktestError("the end date is before the start date")
        if split is not None and not (start < split <= end):
            raise BacktestError("--split must be after the start date and not after the end date")
        contracts = self._store.load_contracts()
        if not contracts:
            raise BacktestError("this snapshot file has no contract archive (it was recorded by an older version, "
                                "or nothing was recorded). Record new data first.")
        master = InstrumentMaster.from_archive(contracts)

        self._work.mkdir(parents=True, exist_ok=True)
        clock = SimClock()
        # State lives ONLY in the work folder: the real risk state / portfolio / journal are never touched.
        risk_cfg = self._risk_cfg.model_copy(update={"state_file": str(self._work / "risk_state.json")})
        paper_cfg = self._exec_cfg.paper if self._exec_cfg.paper.seed is not None else self._exec_cfg.paper.model_copy(update={"seed": 0})
        latest: dict[Instr, Tick] = {}
        quote = lambda token: latest.get(Instr("NFO", token))
        portfolio = Portfolio(quote, self._exec_cfg.fees, None, clock.dt)
        health = FeedHealth(self._stale, clock=clock.epoch)
        health.set_connected(True)
        risk = RiskEngine(risk_cfg, portfolio, quote, health.status, RiskStateStore(risk_cfg.state_file), clock=clock.dt)
        broker = PaperBroker(quote, paper_cfg, clock=clock.epoch)  # the ONLY broker a backtest can have
        mgr = ExecutionManager(risk, broker, portfolio, quote, self._exec_cfg, clock=clock.epoch)
        risk.set_actions(mgr)
        journal = Journal(self._work / "backtest_journal.db")
        risk.add_listener(journal.on_decision)
        mgr.add_listener(journal.on_event)
        strategies = build_strategies(load_strategy_config(self._strategy_path))
        if not strategies:
            raise BacktestError("no strategy is enabled in the strategy config")
        runner: StrategyRunner | None = None

        lo = datetime.combine(start, time.min, IST).timestamp()
        hi = datetime.combine(end + timedelta(days=1), time.min, IST).timestamp()
        quality: dict[str, DayQuality] = {}
        ctx_key: tuple | None = None
        prev_ts: float | None = None
        n = 0

        for snap in self._store.iter_snapshots(lo, hi):
            if not snap.future_token or not snap.index_token:
                raise BacktestError("snapshots lack the index/future tokens (recorded by an older version)")
            n += 1
            gap = 0.0 if prev_ts is None else snap.ts - prev_ts
            day = datetime.fromtimestamp(snap.ts, IST).date().isoformat()
            q = quality.setdefault(day, DayQuality(day, first=snap.ts))
            q.snapshots += 1
            q.last = snap.ts
            if q.snapshots > 1:
                q.max_gap = max(q.max_gap, gap)

            key = (day, snap.expiry, snap.future_token, snap.index_token)
            if key != ctx_key:
                # New day or new expiry: a fresh market view. Positions and rules carry over.
                latest.clear()
                ctx = LiveContext(master, snap.underlying, date.fromisoformat(snap.expiry),
                                  Instr("NFO", snap.future_token), Instr("NSE", snap.index_token), portfolio)
                health.set_critical([ctx.future])
                if runner is None:
                    runner = StrategyRunner(strategies, ctx, mgr.submit)
                else:
                    runner.set_context(ctx)
                ctx_key, prev_ts = key, None

            assert runner is not None
            # Each tick is delivered at ITS OWN time (never later than the snapshot), so an order decided on
            # a tick 0.5 s before the snapshot is timed like it was live; only polling is quantised to
            # snapshots. A new day starts from midnight: after yesterday's data, but before any of today's
            # fresh ticks (the first snapshot of a day also holds stale quotes from the previous session).
            midnight = datetime.combine(datetime.fromtimestamp(snap.ts, IST).date(), time.min, IST).timestamp()
            clock.t = max(clock.t, midnight) if prev_ts is None else prev_ts
            for tick in snapshot_ticks(snap, prev_ts):
                clock.t = max(clock.t, tick.received_at)
                latest[tick.instr] = tick       # what risk, portfolio and execution price against
                health.record_tick(tick.instr)
                runner.on_tick(tick)
            clock.t = snap.ts
            mgr.poll()
            risk.poll()
            prev_ts = snap.ts

        if n == 0:
            raise BacktestError(f"no snapshots between {start} and {end}. Run the system during market hours to record some "
                                f"(python -m trader.journal snapshots shows what you have).")
        mgr.poll()

        summary = journal.summary(start, end)
        parts = {}
        if split is not None:
            parts = {"before": journal.summary(start, split - timedelta(days=1)), "after": journal.summary(split, end)}
        result = BacktestResult(start, end, n, sorted(quality.values(), key=lambda d: d.day), summary, parts,
                                journal.trades(start, end), len(portfolio.open_positions()), work_dir=self._work, journal=journal)
        result.warnings = self._warnings(result)
        return result

    @staticmethod
    def _warnings(r: BacktestResult) -> list[str]:
        w = []
        if len(r.days) < MIN_DAYS_FOR_ANY_CONFIDENCE:
            w.append(f"Only {len(r.days)} recorded day(s); under {MIN_DAYS_FOR_ANY_CONFIDENCE} days says almost nothing about a strategy.")
        if r.summary.trades < MIN_TRADES_FOR_ANY_CONFIDENCE:
            w.append(f"Only {r.summary.trades} trade(s); under {MIN_TRADES_FOR_ANY_CONFIDENCE} is mostly luck.")
        spacings = [(d.last - d.first) / (d.snapshots - 1) for d in r.days if d.snapshots > 1]
        typical = statistics.median(spacings) if spacings else 0.0
        for d in r.days:
            if typical and d.max_gap > max(120.0, 10 * typical):
                w.append(f"{d.day}: recording has a gap of {d.max_gap / 60:.0f} minutes (feed outage or the system was down).")
            if typical and d.snapshots * typical < 0.5 * 6.25 * 3600:
                w.append(f"{d.day}: only {d.snapshots} snapshots (about {d.snapshots * typical / 3600:.1f} h of the 6.25 h session recorded).")
        if r.open_positions:
            w.append(f"{r.open_positions} position(s) were still open when the data ended; their P&L is not in the totals.")
        return w


# ================================================================ comparison

@dataclass
class TradePair:
    key: str
    bt: TradeRow
    paper: TradeRow


@dataclass
class Comparison:
    matched: list[TradePair]
    only_backtest: list[TradeRow]
    only_paper: list[TradeRow]


def compare_with_paper(bt_trades: list[TradeRow], paper_trades: list[TradeRow], strategies: set[str]) -> Comparison:
    """Match trades by the ENTRY signal key (strategy:ENTRY:contract:minute), which is deterministic.

    Only strategy trades are compared; webhook/manual paper trades cannot appear in a strategy replay.
    """
    paper = {t.entry_signal_key: t for t in paper_trades if t.entry_signal_key and t.strategy in strategies}
    bt = {t.entry_signal_key: t for t in bt_trades if t.entry_signal_key}
    matched = [TradePair(k, bt[k], paper[k]) for k in sorted(bt.keys() & paper.keys(), key=lambda k: (bt[k].opened_at, k))]
    return Comparison(matched, [bt[k] for k in sorted(bt.keys() - paper.keys())],
                      [paper[k] for k in sorted(paper.keys() - bt.keys())])


def format_comparison(c: Comparison) -> str:
    L = ["BACKTEST vs PAPER TRADING (same period, strategy trades only)", "=" * 78]
    bt_net = sum(p.bt.net_pnl for p in c.matched) + sum(t.net_pnl for t in c.only_backtest)
    paper_net = sum(p.paper.net_pnl for p in c.matched) + sum(t.net_pnl for t in c.only_paper)
    L.append(f"Trades: {len(c.matched)} in both, {len(c.only_backtest)} only in the backtest, {len(c.only_paper)} only in paper trading")
    L.append(f"Net P&L: backtest Rs {bt_net:+,.0f}   paper Rs {paper_net:+,.0f}   difference Rs {bt_net - paper_net:+,.0f}")
    if c.matched:
        ent = [abs((p.bt.entry_price or 0) - (p.paper.entry_price or 0)) for p in c.matched if p.bt.entry_price and p.paper.entry_price]
        dif = [p.bt.net_pnl - p.paper.net_pnl for p in c.matched]
        L.append(f"On the {len(c.matched)} matched trades: average entry-price difference "
                 f"{(sum(ent) / len(ent)) if ent else 0:.2f}, average P&L difference (backtest - paper) Rs {sum(dif) / len(dif):+,.0f}")
        L.append("")
        L.append(f"  {'opened':<17}{'setup':<36}{'backtest':>10}{'paper':>10}{'diff':>9}")
        for p in c.matched[:15]:
            L.append(f"  {p.bt.opened_at[:16].replace('T', ' '):<17}{p.bt.setup[:35]:<36}{p.bt.net_pnl:>+10,.0f}{p.paper.net_pnl:>+10,.0f}{p.bt.net_pnl - p.paper.net_pnl:>+9,.0f}")
    for label, rows in (("Only in the backtest", c.only_backtest), ("Only in paper trading", c.only_paper)):
        if rows:
            L.append("")
            L.append(f"{label}:")
            for t in rows[:10]:
                L.append(f"  {t.opened_at[:16].replace('T', ' ')}  {t.setup[:40]:<40} net Rs {t.net_pnl:+,.0f}")
    L.append("")
    L.append("Differences are expected: the replay sees sampled snapshots, paper trading saw every tick. Large,")
    L.append("persistent differences mean the replay is not a faithful model of what paper trading does.")
    return "\n".join(L)


# ====================================================================== CLI

def _day(s: str) -> date:
    return date.fromisoformat(s)


def _cli(argv: list[str] | None = None, out=print, today: date | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trader.backtest", description="Replay recorded snapshots (see the limits!)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="replay a date range")
    r.add_argument("--from", dest="start", type=_day, required=True)
    r.add_argument("--to", dest="end", type=_day, help="default: today")
    r.add_argument("--split", type=_day, help="report 'before' and 'after' this date separately (out-of-sample check)")
    r.add_argument("--market", default="data/market.db", help="snapshot database")
    r.add_argument("--strategy-config", default="config/strategy.yaml", help="rules to replay (a variant file kept in git)")
    r.add_argument("--paper-journal", help="journal.db of real paper trading, to compare against")
    r.add_argument("--csv", help="write the backtest trades to this CSV file")
    r.add_argument("--work-dir", help="where replay state goes (default: a temporary folder)")
    args = ap.parse_args(argv)

    out(LIMITS_TEXT + "\n")
    if not Path(args.market).exists():
        out(f"No snapshot file at {args.market}. Run the system during market hours first.")
        return 2
    store = SnapshotStore(args.market)
    end = args.end or today or datetime.now(IST).date()
    try:
        res = Backtester(store, args.work_dir, args.strategy_config).run(args.start, end, args.split)
    except BacktestError as e:
        out(f"Cannot backtest: {e}")
        return 1
    out(f"Replayed {res.snapshots:,} snapshots over {len(res.days)} day(s): "
        + ", ".join(f"{d.day} ({d.snapshots:,})" for d in res.days[:8]) + (" ..." if len(res.days) > 8 else ""))
    out("")
    out(format_summary(res.summary, title="BACKTEST SUMMARY", note="(SIMULATED replay, not real trading)"))
    if res.parts:
        out("")
        out(f"OUT-OF-SAMPLE CHECK around {args.split}: results that only hold BEFORE the split are suspect.")
        for name, s in res.parts.items():
            wr = "n/a" if s.win_rate is None else f"{s.win_rate * 100:.0f}%"
            out(f"  {name:<7} {s.start} to {s.end}: {s.trades} trades, win rate {wr}, net Rs {s.net_pnl:+,.0f}, "
                f"max drawdown Rs {s.max_drawdown:,.0f}")
    for w in res.warnings:
        out(f"WARNING: {w}")
    if args.paper_journal:
        if not Path(args.paper_journal).exists():
            out(f"\n(No paper journal at {args.paper_journal}; skipping the comparison.)")
        else:
            paper = Journal(args.paper_journal).trades(args.start, end)
            names = {t.strategy for t in res.trades} | {s.name for s in build_strategies(load_strategy_config(args.strategy_config))}
            out("")
            out(format_comparison(compare_with_paper(res.trades, paper, names)))
    if args.csv and res.journal is not None:
        n = res.journal.export_csv(args.csv, args.start, end)
        out(f"\nWrote {n} backtest trade(s) to {args.csv}")
    out(f"\n(Replay state is in {res.work_dir}; it never touches your real data.)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(_cli())
