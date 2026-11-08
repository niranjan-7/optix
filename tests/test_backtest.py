import inspect
import math
from pathlib import Path
from datetime import date, datetime, timedelta

import pytest
import yaml

from tests.test_instruments import sample_rows
from tests.test_strategy import INDEX_ROW
from trader import backtest as bt
from trader.analytics import format_summary
from trader.backtest import (
    Backtester, BacktestError, SimClock, compare_with_paper, format_comparison, snapshot_ticks,
)
from trader.brokers import PaperBroker
from trader.execution import ExecutionManager, load_execution_config
from trader.feed import FeedHealth, Instr, Tick
from trader.greeks import DEFAULT_RATE, IST, bs_price, time_to_expiry
from trader.instruments import InstrumentMaster, InstrumentError
from trader.journal import Journal, TradeRow
from trader.portfolio import Portfolio
from trader.risk import RiskEngine, RiskStateStore, load_risk_config
from trader.snapshots import Quote, Snapshot, SnapshotRecorder, SnapshotStore
from trader.strategy import LiveContext, StrategyRunner, build_strategies, load_strategy_config

MASTER = InstrumentMaster.from_rows(sample_rows() + [INDEX_ROW], {"NIFTY"})
EXP = date(2026, 11, 17)
IDX, FUT = Instr("NSE", "99926000"), Instr("NFO", "58875")
DAY1, DAY2 = date(2026, 11, 10), date(2026, 11, 11)     # a Tuesday and a Wednesday
OPTS = [o for o in MASTER._options if o.expiry == EXP]


def at(day, h, m, s=0.0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST).timestamp() + s


# ------------------------------------------------------------ a synthetic session

def session(day, start=(9, 30), end=(10, 45), skip=None, climb=8.0, only=None):
    """[(t, ticks_before_the_future, future_tick)]: flat, then a climb. Options priced by Black-76 at 6% vol
    (cheap enough to fit risk.yaml). Option/index ticks land 0.5 s before the future tick, which is what
    makes a recorded snapshot complete (the recorder fires on the future tick)."""
    steps = []
    t0, t1 = at(day, *start), at(day, *end)
    t = t0
    while t < t1:
        if skip and skip[0] <= t < skip[1]:
            t += 5.0
            continue
        mins = (t - at(day, 9, 30)) / 60
        spot = 25000 + 2 * math.sin(mins) + climb * max(0.0, min(mins - 15, 15))
        fut = spot + 30
        T = time_to_expiry(datetime.fromtimestamp(t, IST), EXP)
        before = [Tick("NSE", IDX.token, spot, None, None, None, None, None, t - 0.5)]
        for o in OPTS:
            if only and o.opt_type != only:
                continue
            px = max(0.05, round(bs_price(fut, o.strike, T, DEFAULT_RATE, 0.06, o.opt_type, b=0.0) / 0.05) * 0.05)
            before.append(Tick("NFO", o.token, px, px - 0.25, px + 0.25, 100, 500, None, t - 0.5))
        steps.append((t, before, Tick("NFO", FUT.token, fut, fut - 0.25, fut + 0.25, None, None, None, t)))
        t += 5.0
    return steps


def record(store, steps):
    """Feed the real recorder. A fresh recorder per day (like a daily restart), so each day's first snapshot is
    taken on the future tick, after that step's option/index ticks have arrived."""
    rec, day = None, None
    for t, before, fut in steps:
        d = datetime.fromtimestamp(t, IST).date()
        if d != day:
            rec = SnapshotRecorder(store, "NIFTY", EXP, IDX, FUT, 5.0, describe=lambda ins: MASTER.describe(ins.exchange, ins.token))
            day = d
        for k, tick in enumerate(before):
            rec.on_tick(tick)
        rec.on_tick(fut)
    return rec


def run_live(steps, work, strategy_path="config/strategy.yaml"):
    """The reference: the same pipeline fed tick by tick, as paper trading would see them."""
    clock, latest = SimClock(), {}
    quote = lambda token: latest.get(Instr("NFO", token))
    ecfg = load_execution_config()
    pf = Portfolio(quote, ecfg.fees, None, clock.dt)
    health = FeedHealth(10, clock=clock.epoch); health.set_connected(True)
    rcfg = load_risk_config().model_copy(update={"state_file": str(work / "live_risk.json")})
    risk = RiskEngine(rcfg, pf, quote, health.status, RiskStateStore(rcfg.state_file), clock=clock.dt)
    broker = PaperBroker(quote, ecfg.paper.model_copy(update={"seed": 0}), clock=clock.epoch)
    mgr = ExecutionManager(risk, broker, pf, quote, ecfg, clock=clock.epoch)
    risk.set_actions(mgr)
    j = Journal(work / "live_journal.db")
    risk.add_listener(j.on_decision); mgr.add_listener(j.on_event)
    ctxs: dict[str, StrategyRunner] = {}
    runner = None
    for t, before, fut in steps:
        day = datetime.fromtimestamp(t, IST).date().isoformat()
        if day not in ctxs:
            latest.clear()
            ctx = LiveContext(MASTER, "NIFTY", EXP, FUT, IDX, pf)
            health.set_critical([FUT])
            if runner is None:
                runner = StrategyRunner(build_strategies(load_strategy_config(strategy_path)), ctx, mgr.submit)
            else:
                runner.set_context(ctx)
            ctxs[day] = runner
        clock.t = t - 0.5
        for tick in before:
            latest[tick.instr] = tick; health.record_tick(tick.instr); runner.on_tick(tick)
        clock.t = t
        latest[fut.instr] = fut; health.record_tick(fut.instr); runner.on_tick(fut)
        mgr.poll(); risk.poll()
    return j


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    d = tmp_path_factory.mktemp("bt")
    steps = session(DAY1)
    store = SnapshotStore(d / "market.db")
    record(store, steps)
    live = run_live(steps, d)
    return d, store, live, steps


def key_pnl(trades):
    return [(t.entry_signal_key, round(t.net_pnl, 4), round(t.entry_price, 4), round(t.exit_price, 4)) for t in trades]


# ===================================================== archive and tick rebuilding

def test_recorder_archives_each_contract_once_and_the_master_can_be_rebuilt(tmp_path):
    store = SnapshotStore(tmp_path / "m.db")
    rec = record(store, session(DAY1, end=(9, 31)))
    rows = store.load_contracts()
    kinds = sorted({r["kind"] for r in rows})
    assert kinds == ["FUT", "IDX", "OPT"] and len(rows) == len(OPTS) + 2          # one row per token, not per snapshot
    assert store.register_contracts(rows) == 0                                    # insert-only
    m = InstrumentMaster.from_archive(rows)
    o = m.find_option("NIFTY", EXP, 25000, "CE")
    assert (o.symbol, o.lot_size, o.tick_size, o.token) == ("NIFTY17NOV2625000CE", 65, 0.05, "25000CE17")
    assert m.index_token("NIFTY") == "99926000" and m.nearest_future("NIFTY", DAY1).token == "58875"
    assert [x.strike for x in m.chain("NIFTY", EXP, 25001, 1) if x.opt_type == "CE"] == [24950, 25000, 25050]
    snap = next(store.iter_snapshots())
    assert (snap.index_token, snap.future_token) == ("99926000", "58875")


def test_describe_and_archive_edge_cases():
    assert MASTER.describe("NFO", "nope") is None and MASTER.describe("NSE", "nope") is None
    assert MASTER.describe("NSE", "99926000")["kind"] == "IDX"
    with pytest.raises(InstrumentError):
        InstrumentMaster.from_archive([])


def snap(ts, quotes, spot=25000.0, fut_token="58875"):
    return Snapshot(ts, "NIFTY", "2026-11-17", spot, 25030.0, quotes, "99926000", fut_token)


def test_snapshot_ticks_reemit_only_what_changed_and_keep_ages_honest():
    q = lambda ltp, age: Quote(ltp, ltp - 0.25, ltp + 0.25, 1, 2, age)
    s1 = snap(1000.0, {"NFO:1": q(100, 0.5), "NFO:58875": q(25030, 0.0), "NFO:2": q(50, 3.0)})
    first = snapshot_ticks(s1, None)                                   # first snapshot: everything is new
    assert [t.token for t in first] == ["2", "1", "99926000", "58875"]        # oldest first; the future last among equals
    assert {t.token for t in first} == {"1", "2", "58875", "99926000"}
    s2 = snap(1005.0, {"NFO:1": q(101, 0.5), "NFO:58875": q(25031, 0.0), "NFO:2": q(50, 8.0)})   # token 2 has not updated since 997
    second = snapshot_ticks(s2, 1000.0)
    assert {t.token for t in second} == {"1", "58875", "99926000"}
    tick1 = next(t for t in second if t.token == "1")
    assert tick1.received_at == pytest.approx(1004.5) and tick1.ltp == 101 and (tick1.bid, tick1.ask) == (100.75, 101.25)
    assert [t.token for t in second] == ["1", "99926000", "58875"]       # the future comes after the quotes it depends on
    spot = next(t for t in second if t.token == "99926000")
    assert spot.exchange == "NSE" and spot.ltp == 25000.0 and spot.bid is None


def test_ticks_are_ordered_oldest_first_and_never_from_the_future():
    q = lambda age: Quote(100, 99.75, 100.25, None, None, age)
    s = snap(2000.0, {"NFO:58875": q(0.0), "NFO:5": q(4.0), "NFO:6": q(0.5)})
    ticks = snapshot_ticks(s, None)
    assert [t.received_at for t in ticks] == sorted(t.received_at for t in ticks)
    assert all(t.received_at <= s.ts for t in ticks)


# ================================================================= the replay

def test_replay_reproduces_what_live_paper_trading_did_from_the_same_ticks(recorded, tmp_path):
    d, store, live, steps = recorded
    res = Backtester(store, tmp_path / "work").run(DAY1, DAY1)
    live_trades = live.trades(DAY1, DAY1)
    assert len(live_trades) >= 1, "the synthetic session must produce at least one trade or this test proves nothing"
    assert key_pnl(res.trades) == key_pnl(live_trades)                 # same entries, same prices, same P&L
    assert res.summary.net_pnl == pytest.approx(live.summary(DAY1, DAY1).net_pnl)
    assert res.snapshots == len(steps) and res.open_positions == len(live_trades) * 0 + res.open_positions
    t = res.trades[0]
    assert t.strategy == "example_momentum" and t.setup == "example_momentum | LONG_OPTION | CE" and t.data_complete


def test_replay_cannot_see_the_future(recorded, tmp_path):
    """Decisions must depend only on data up to 'now': cutting the recording short cannot change what happened before the cut."""
    d, store, live, steps = recorded
    full = Backtester(store, tmp_path / "w1").run(DAY1, DAY1)
    cut_ts = at(DAY1, 10, 20)
    short = SnapshotStore(tmp_path / "short.db")
    short.register_contracts(store.load_contracts())
    for s in store.iter_snapshots(None, cut_ts):
        short.write(s.ts, s.underlying, s.expiry, s.spot, s.future,
                    {k: [q.ltp, q.bid, q.ask, q.volume, q.oi, q.age] for k, q in s.quotes.items()}, s.index_token, s.future_token)
    part = Backtester(short, tmp_path / "w2").run(DAY1, DAY1)
    early = lambda trades: [x for x in trades if x.closed_at < datetime.fromtimestamp(cut_ts, IST).isoformat()]
    assert len(early(full.trades)) >= 1 and key_pnl(early(full.trades)) == key_pnl(early(part.trades))
    assert part.snapshots < full.snapshots


def test_two_days_with_a_new_market_view_each_day(tmp_path):
    steps = session(DAY1) + session(DAY2, climb=-8.0)                  # day 2 falls: a put trade
    store = SnapshotStore(tmp_path / "m.db"); record(store, steps)
    res = Backtester(store, tmp_path / "w").run(DAY1, DAY2)
    live = run_live(steps, tmp_path)
    assert [d.day for d in res.days] == ["2026-11-10", "2026-11-11"]
    assert key_pnl(res.trades) == key_pnl(live.trades(DAY1, DAY2))
    assert {t.setup.split(" | ")[-1] for t in res.trades} == {"CE", "PE"}


def test_backtest_state_stays_in_its_own_folder_and_it_can_only_ever_paper_trade(recorded, tmp_path, monkeypatch):
    d, store, live, steps = recorded
    cfg = {"risk_config": load_risk_config(), "execution_config": load_execution_config(), "stale_seconds": 10.0,
           "strategy_config_path": str(Path(__file__).parent.parent / "config" / "strategy.yaml")}
    monkeypatch.chdir(tmp_path)                                         # any stray default-path write would land here
    work = tmp_path / "isolated"
    Backtester(store, work, **cfg).run(DAY1, DAY1)
    assert (work / "risk_state.json").exists() and (work / "backtest_journal.db").exists()
    assert not (tmp_path / "data").exists()                            # nothing written to a default data/ folder
    src = inspect.getsource(bt)
    for forbidden in ("AngelBroker", "confirm_live_trading", "build_broker", "Alerts", "telegram", "create_app"):
        assert forbidden not in src, forbidden


def test_a_kill_switch_left_on_in_real_life_is_not_inherited(recorded, tmp_path):
    """The replay has its own risk state; the real system's kill switch / halted day cannot leak in or out."""
    d, store, live, steps = recorded
    res = Backtester(store, tmp_path / "w").run(DAY1, DAY1)
    import json
    state = json.load(open(tmp_path / "w" / "risk_state.json"))
    assert state["kill_active"] is False and res.summary.trades >= 1


def test_the_same_risk_rules_apply_in_the_replay(recorded, tmp_path):
    d, store, live, steps = recorded
    risk = load_risk_config()
    tight = risk.model_copy(update={"limits": risk.limits.model_copy(update={"max_loss_per_trade": 100.0})})
    res = Backtester(store, tmp_path / "w", risk_config=tight).run(DAY1, DAY1)
    assert res.summary.trades == 0 and res.summary.rejections_by_code.get("MAX_LOSS_PER_TRADE", 0) >= 1


def test_a_variant_rule_file_changes_the_result(recorded, tmp_path):
    d, store, live, steps = recorded
    raw = yaml.safe_load(open("config/strategy.yaml"))
    raw["strategies"][0]["params"]["move_points"] = 500                # an impossible threshold
    p = tmp_path / "variant.yaml"; p.write_text(yaml.safe_dump(raw))
    res = Backtester(store, tmp_path / "w", strategy_config_path=p).run(DAY1, DAY1)
    assert res.summary.trades == 0
    raw["strategies"][0]["enabled"] = False
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(BacktestError, match="no strategy"):
        Backtester(store, tmp_path / "w2", strategy_config_path=p).run(DAY1, DAY1)


def test_split_reports_before_and_after_that_add_up(tmp_path):
    steps = session(DAY1) + session(DAY2)
    store = SnapshotStore(tmp_path / "m.db"); record(store, steps)
    res = Backtester(store, tmp_path / "w").run(DAY1, DAY2, split=DAY2)
    b, a = res.parts["before"], res.parts["after"]
    assert (b.start, b.end, a.start, a.end) == (DAY1, DAY1, DAY2, DAY2)
    assert b.trades + a.trades == res.summary.trades and b.net_pnl + a.net_pnl == pytest.approx(res.summary.net_pnl)


# ------------------------------------------------------------ data-quality warnings

def test_warnings_flag_thin_data_gaps_and_tiny_samples(tmp_path):
    gap = (at(DAY1, 10, 0), at(DAY1, 10, 25))
    store = SnapshotStore(tmp_path / "m.db"); record(store, session(DAY1, skip=gap))
    res = Backtester(store, tmp_path / "w").run(DAY1, DAY1)
    text = "\n".join(res.warnings)
    assert "Only 1 recorded day(s)" in text and "gap of 25 minutes" in text and "mostly luck" in text
    sparse = SnapshotStore(tmp_path / "s.db"); record(sparse, session(DAY1, end=(9, 50)))
    assert "only" in "\n".join(Backtester(sparse, tmp_path / "w2").run(DAY1, DAY1).warnings).lower()


def test_open_positions_at_the_end_of_the_data_are_called_out(tmp_path):
    steps = session(DAY1, end=(9, 55))                                  # data ends mid-trade
    store = SnapshotStore(tmp_path / "m.db"); record(store, steps)
    res = Backtester(store, tmp_path / "w").run(DAY1, DAY1)
    assert res.open_positions == 1 and any("still open when the data ended" in w for w in res.warnings)


# --------------------------------------------------------------------- errors

def test_clear_errors_when_the_replay_cannot_be_meaningful(tmp_path):
    empty = SnapshotStore(tmp_path / "e.db")
    with pytest.raises(BacktestError, match="no contract archive"):
        Backtester(empty, tmp_path / "w").run(DAY1, DAY1)
    store = SnapshotStore(tmp_path / "m.db"); record(store, session(DAY1, end=(9, 40)))
    with pytest.raises(BacktestError, match="no snapshots between"):
        Backtester(store, tmp_path / "w").run(date(2027, 1, 4), date(2027, 1, 5))
    with pytest.raises(BacktestError, match="before the start"):
        Backtester(store, tmp_path / "w").run(DAY2, DAY1)
    for bad in (DAY1, DAY2 + timedelta(days=5)):
        with pytest.raises(BacktestError, match="--split"):
            Backtester(store, tmp_path / "w").run(DAY1, DAY2, split=bad)
    old = SnapshotStore(tmp_path / "old.db")                            # snapshots without tokens (older recorder)
    old.register_contracts(store.load_contracts())
    old.write(at(DAY1, 10, 0), "NIFTY", "2026-11-17", 25000.0, 25030.0, {"NFO:1": [1, None, None, None, None, 0.0]})
    with pytest.raises(BacktestError, match="older version"):
        Backtester(old, tmp_path / "w").run(DAY1, DAY1)


# ----------------------------------------------------------------- comparison

def trade(i, key, pnl, strategy="example_momentum", entry=100.0, exit_=110.0, opened="2026-11-10T09:49:05+05:30"):
    return TradeRow(position_id=f"P{i}", strategy=strategy, setup=f"{strategy} | LONG_OPTION | CE", opened_at=opened,
                    closed_at=opened, hold_minutes=5, lots=1, entry_signal_key=key, entry_price=entry, exit_price=exit_,
                    gross_pnl=pnl, fees=0, net_pnl=pnl, slippage_cost=0)


def test_comparison_matches_by_entry_key_and_ignores_trades_a_replay_cannot_contain():
    bt_t = [trade(1, "a", 500), trade(2, "b", -200, entry=90), trade(3, "only-bt", 50)]
    paper = [trade(1, "a", 450), trade(2, "b", -250, entry=91), trade(3, "only-paper", 70),
             trade(4, "webhook:x", 999, strategy="webhook")]
    c = compare_with_paper(bt_t, paper, {"example_momentum"})
    assert [p.key for p in c.matched] == ["a", "b"]
    assert [t.entry_signal_key for t in c.only_backtest] == ["only-bt"] and [t.entry_signal_key for t in c.only_paper] == ["only-paper"]
    text = format_comparison(c)
    assert "2 in both, 1 only in the backtest, 1 only in paper" in text
    assert "backtest Rs +350" in text and "paper Rs +270" in text and "difference Rs +80" in text    # webhook trade excluded
    assert "average entry-price difference 0.50" in text and "average P&L difference (backtest - paper) Rs +50" in text
    assert "Differences are expected" in text


def test_comparison_with_nothing_to_compare_is_graceful():
    text = format_comparison(compare_with_paper([], [], {"x"}))
    assert "0 in both" in text and "Rs +0" in text


def test_a_faithful_replay_matches_every_paper_trade(recorded, tmp_path):
    d, store, live, steps = recorded
    res = Backtester(store, tmp_path / "w").run(DAY1, DAY1)
    c = compare_with_paper(res.trades, live.trades(DAY1, DAY1), {"example_momentum"})
    assert len(c.matched) == len(res.trades) >= 1 and not c.only_backtest and not c.only_paper
    assert all(abs(p.bt.net_pnl - p.paper.net_pnl) < 1e-6 for p in c.matched)


# ------------------------------------------------------------------------ CLI

def cli(args, tmp_path, today=DAY2):
    out = []
    code = bt._cli(args, out=out.append, today=today)
    return code, "\n".join(out)


def test_cli_end_to_end_prints_limits_summary_comparison_and_csv(recorded, tmp_path):
    d, store, live, steps = recorded
    csv_path = tmp_path / "bt.csv"
    code, out = cli(["run", "--from", "2026-11-10", "--to", "2026-11-10", "--market", str(d / "market.db"),
                     "--paper-journal", str(d / "live_journal.db"), "--csv", str(csv_path),
                     "--work-dir", str(tmp_path / "w")], tmp_path)
    assert code == 0
    assert out.startswith("BACKTEST - READ THE LIMITS")                  # the limits come BEFORE any number
    for needle in ("OVERFITTING", "Replayed ", "BACKTEST SUMMARY", "(SIMULATED replay, not real trading)",
                   "BACKTEST vs PAPER TRADING", "in both, 0 only in the backtest, 0 only in paper", "WARNING: Only 1 recorded day",
                   "never touches your real data"):
        assert needle in out, needle
    assert "PAPER results unless you went live" not in out               # cannot be mistaken for paper trading
    assert csv_path.exists() and csv_path.read_text().count("\n") >= 2


def test_cli_split_prints_an_out_of_sample_check(tmp_path):
    steps = session(DAY1) + session(DAY2)
    store = SnapshotStore(tmp_path / "market.db"); record(store, steps)
    code, out = cli(["run", "--from", "2026-11-10", "--split", "2026-11-11", "--market", str(tmp_path / "market.db"),
                     "--work-dir", str(tmp_path / "w")], tmp_path)
    assert code == 0 and "OUT-OF-SAMPLE CHECK around 2026-11-11" in out and "before" in out and "after" in out


def test_cli_failures_are_explained(tmp_path):
    code, out = cli(["run", "--from", "2026-11-10", "--market", str(tmp_path / "missing.db")], tmp_path)
    assert code == 2 and "No snapshot file" in out and "READ THE LIMITS" in out
    store = SnapshotStore(tmp_path / "market.db"); record(store, session(DAY1, end=(9, 40)))
    code, out = cli(["run", "--from", "2027-01-04", "--to", "2027-01-05", "--market", str(tmp_path / "market.db")], tmp_path)
    assert code == 1 and "Cannot backtest: no snapshots between" in out


def test_summary_title_is_configurable_so_a_backtest_cannot_pass_for_live_results(recorded, tmp_path):
    d, store, live, steps = recorded
    s = Backtester(store, tmp_path / "w").run(DAY1, DAY1).summary
    assert format_summary(s).startswith("PERFORMANCE SUMMARY") and "PAPER results" in format_summary(s)
    assert format_summary(s, "BACKTEST SUMMARY", "(SIMULATED)").startswith("BACKTEST SUMMARY")


def test_a_quote_that_updated_just_after_the_previous_snapshot_is_not_lost():
    """Real feeds tick continuously: a quote 30 ms after the last snapshot must reach the next replay step."""
    q = lambda age: Quote(100, 99.75, 100.25, None, None, age)
    prev = 1000.0
    s = snap(1005.0, {"NFO:1": q(4.97), "NFO:2": q(5.0), "NFO:58875": q(0.0)})      # updated at 1000.03 and 1000.00
    tokens = {t.token for t in snapshot_ticks(s, prev)}
    assert "1" in tokens                    # 1000.03 > 1000.00: new
    assert "2" not in tokens                # exactly the snapshot instant: it was already in that snapshot


def test_yesterdays_quotes_do_not_survive_into_a_new_day(tmp_path):
    """Models the daily restart: a position left open overnight whose contract is missing from the next day's data
    must stay open (with a warning) - not be closed at a day-old price."""
    steps = session(DAY1) + session(DAY2, end=(15, 25), climb=0.0, only="PE")          # day 2 has no calls at all
    store = SnapshotStore(tmp_path / "m.db"); record(store, steps)
    res = Backtester(store, tmp_path / "w").run(DAY1, DAY2)
    assert res.open_positions == 1 and any("still open when the data ended" in w for w in res.warnings)
    with res.journal._session() as s:
        warned = [r[0] for r in s.execute(__import__("sqlalchemy").text("select payload from events where type = 'warning'"))]
    assert any("no market quote to price the order" in w for w in warned)
