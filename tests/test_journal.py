import csv
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from tests.exec_helpers import TUE, World, leg
from trader.greeks import IST
from trader.journal import (
    Journal, SignalRow, TradeRow, _cli, load_journal_config, review_interactively,
)
from trader.snapshots import SnapshotStore

DAY = TUE.date()  # 2026-11-10
ALL = ["LONG_OPTION", "VERTICAL_DEBIT", "VERTICAL_CREDIT", "IRON_CONDOR"]


def wired(tmp_path, **world_kw):
    w = World(tmp_path / "w", **world_kw)
    j = Journal(tmp_path / "journal.db")
    w.risk.add_listener(j.on_decision)
    w.mgr.add_listener(j.on_event)
    return w, j


def round_trip(w, key="e1", exit_key="x1"):
    assert w.mgr.submit(w.entry(key)).approved
    w.advance(1)
    assert w.mgr.submit(w.exit(exit_key)).approved
    w.advance(1)


# ============================================================ trade building

def test_round_trip_becomes_a_trade_with_hand_checked_numbers(tmp_path):
    w, j = wired(tmp_path)
    round_trip(w)
    (t,) = j.trades()
    # entry: buy at ask 100.25 + 1 tick = 100.30 ; exit: sell at bid 99.75 - 1 tick = 99.70 ; 65 units ; Rs20 per order
    assert t.entry_price == pytest.approx(100.30) and t.exit_price == pytest.approx(99.70)
    assert t.gross_pnl == pytest.approx(-0.60 * 65) and t.fees == 40 and t.net_pnl == pytest.approx(-39 - 40)
    assert t.lots == 1 and t.data_complete is True
    assert t.setup == "ex | LONG_OPTION | CE" and t.structure == "LONG_OPTION" and t.position_id == "P1"
    assert (t.entry_reason, t.exit_reason) == ("test", "test exit")
    assert t.entry_signal_key == "e1" and t.exit_signal_key == "x1"
    assert t.hold_minutes == pytest.approx(1.0 / 60, abs=0.01)
    assert t.opened_at < t.closed_at and t.closed_at.startswith("2026-11-10T10:30")
    # the journal agrees with the portfolio's own books
    assert t.net_pnl == pytest.approx(w.portfolio.realised_pnl_today())


def test_slippage_is_recorded_even_though_the_exit_reports_after_the_position_closes(tmp_path):
    w, j = wired(tmp_path)
    round_trip(w)
    (t,) = j.trades()
    # buy: paid 100.30 vs quote 100.25 -> +0.05 ; sell: got 99.70 vs quote 99.75 -> +0.05 ; both x 65 units
    assert t.slippage_cost == pytest.approx(0.05 * 65 * 2)


def test_every_signal_is_recorded_with_its_outcome_and_reasons(tmp_path):
    w, j = wired(tmp_path)
    w.mgr.submit(w.entry("good"))
    w.mgr.submit(w.entry("nosl", sl=None))
    w.mgr.submit(w.entry("good"))                                  # replay of an approved key
    with j._session() as s:
        rows = list(s.query(SignalRow).order_by(SignalRow.id))
    assert [(r.idempotency_key, r.approved, r.codes) for r in rows] == [
        ("good", True, ""), ("nosl", False, "NO_STOP_LOSS"), ("good", False, "DUPLICATE")]
    ok = rows[0]
    assert ok.structure == "LONG_OPTION" and ok.loss_at_stop == pytest.approx((100.25 - 75.2) * 65)
    assert ok.stop_loss == 75.2 and ok.target == 140.0 and ok.kind == "ENTRY" and json.loads(ok.legs_json)[0]["strike"] == 25000
    assert "no stop-loss" in rows[1].detail and rows[1].ts.startswith("2026-11-10T10:30")


def test_credit_spread_trade_is_valued_per_structure(tmp_path):
    w, j = wired(tmp_path, structures=ALL, px={"25000CE": (110.0, 0.5), "25100CE": (60.0, 0.5)})
    open_ = w.entry("cs", legs=[leg("SELL", 25000), leg("BUY", 25100)], sl=80.0, target=10.0)
    assert w.mgr.submit(open_).approved
    w.advance(3)
    assert w.mgr.submit(w.exit("csx", legs=[leg("BUY", 25000), leg("SELL", 25100)])).approved
    w.advance(3)
    (t,) = j.trades()
    # open: sold 25000 @109.70, bought 25100 @60.30 -> credit 49.40/unit
    # close: bought back 25000 @110.30, sold 25100 @59.70 -> paid 50.60/unit
    assert t.structure == "VERTICAL_CREDIT" and t.setup == "ex | VERTICAL_CREDIT | CE" and t.lots == 1
    assert t.entry_price == pytest.approx(-49.40) and t.exit_price == pytest.approx(-50.60)
    assert t.gross_pnl == pytest.approx(-1.20 * 65) and t.fees == 80 and t.net_pnl == pytest.approx(-78 - 80)
    assert t.data_complete


def test_trade_for_a_position_the_journal_never_saw_open_is_flagged_incomplete(tmp_path):
    w = World(tmp_path / "w")
    w.mgr.submit(w.entry("e1")); w.advance(1)                      # journal not attached yet (e.g. started late / was down)
    j = Journal(tmp_path / "j.db")
    w.mgr.add_listener(j.on_event); w.risk.add_listener(j.on_decision)
    w.mgr.submit(w.exit("x1")); w.advance(1)
    (t,) = j.trades()
    assert t.data_complete is False and t.net_pnl == pytest.approx(w.portfolio.realised_pnl_today())   # P&L still right
    assert t.entry_price is None


def test_other_events_are_kept_for_audit(tmp_path):
    w, j = wired(tmp_path, paper={"reject_probability": 1.0, "seed": 1})
    w.mgr.submit(w.entry()); w.advance(1)
    with j._session() as s:
        types = [r[0] for r in s.execute(__import__("sqlalchemy").text("select type from events"))]
    assert "warning" in types and "order_placed" in types and "execution_started" in types


def test_a_broken_database_never_disturbs_trading(tmp_path, caplog):
    w, j = wired(tmp_path)
    j._session = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error"))
    w.mgr.submit(w.entry()); w.advance(1)                          # must not raise
    assert len(w.portfolio.open_positions()) == 1                  # trading carried on
    assert "Journal could not record" in caplog.text


def test_journal_survives_reopening_and_guards_its_schema(tmp_path):
    w, j = wired(tmp_path)
    round_trip(w)
    j2 = Journal(tmp_path / "journal.db")
    assert len(j2.trades()) == 1
    c = sqlite3.connect(tmp_path / "journal.db"); c.execute("PRAGMA user_version = 42"); c.commit(); c.close()
    with pytest.raises(RuntimeError, match="different version"):
        Journal(tmp_path / "journal.db")


# ======================================================== evening review

def trade_row(i, pnl, closed="2026-11-10T11:00:00+05:30", followed=None, setup="s"):
    return TradeRow(position_id=f"P{i}", strategy="st", setup=setup, opened_at=closed, closed_at=closed, hold_minutes=5,
                    lots=1, gross_pnl=pnl, fees=0.0, net_pnl=pnl, slippage_cost=0.0, followed_rules=followed)


def seeded(tmp_path, rows):
    j = Journal(tmp_path / "j.db")
    with j._session() as s:
        s.add_all(rows); s.commit()
    return j


def test_review_fields_start_empty_and_notes_accumulate(tmp_path):
    j = seeded(tmp_path, [trade_row(1, 100)])
    (t,) = j.trades()
    assert t.followed_rules is None and t.review_notes == "" and t.reviewed_at is None
    j.review(t.id, False, "moved my stop by hand", "uncle", now=TUE)
    j.review(t.id, None, "second thought: market was gapping", "uncle", now=TUE + timedelta(hours=1))
    (t,) = j.trades()
    assert t.followed_rules is False and t.reviewed_by == "uncle"
    assert t.review_notes == "moved my stop by hand\nsecond thought: market was gapping"
    assert t.reviewed_at == (TUE + timedelta(hours=1)).isoformat()
    assert j.trades(unreviewed_only=True) == []
    with pytest.raises(KeyError):
        j.review(999, True, "", "uncle")


def test_interactive_review_flow(tmp_path):
    j = seeded(tmp_path, [trade_row(1, 100), trade_row(2, -50), trade_row(3, 20), trade_row(4, 5)])
    answers = iter(["maybe", "y", "good entry",        # invalid answer re-asked, then yes + note
                    "s",                               # skip trade 2
                    "n", "",                           # trade 3: no, no note
                    "q"])                              # quit before trade 4
    out = []
    n = review_interactively(j, "uncle", lambda prompt: next(answers), out.append, now=TUE)
    assert n == 2
    t1, t2, t3, t4 = j.trades()
    assert (t1.followed_rules, t1.review_notes) == (True, "good entry")
    assert t2.followed_rules is None and t3.followed_rules is False and t4.followed_rules is None
    assert any("Please type y, n, s or q" in line for line in out)
    assert review_interactively(seeded(tmp_path / "x", []), "u", lambda p: "", out.append) == 0


# ============================================================== summary

def test_summary_filters_by_period_and_counts_signals(tmp_path):
    j = seeded(tmp_path, [
        trade_row(1, 500, "2026-11-02T10:00:00+05:30", True), trade_row(2, -200, "2026-11-03T10:00:00+05:30", False),
        trade_row(3, 100, "2026-11-09T15:29:59+05:30"), trade_row(4, 999, "2026-11-10T00:00:00+05:30"),   # outside
        trade_row(5, 999, "2026-11-01T23:59:59+05:30")])                                                    # outside
    s = j.summary(date(2026, 11, 2), date(2026, 11, 9))
    assert (s.trades, s.net_pnl, s.wins, s.losses) == (3, 400, 2, 1)
    assert s.reviewed == 2 and (s.followed_yes, s.followed_no) == (1, 1)


def test_summary_signal_counts_come_from_the_risk_decisions(tmp_path):
    w, j = wired(tmp_path)
    w.mgr.submit(w.entry("a")); w.mgr.submit(w.entry("b", sl=None)); w.mgr.submit(w.entry("c", sl=None))
    s = j.summary(DAY, DAY)
    assert (s.signals_total, s.signals_approved) == (3, 1) and s.rejections_by_code == {"NO_STOP_LOSS": 2}
    assert j.summary(DAY + timedelta(days=1), DAY + timedelta(days=2)).signals_total == 0


# ===================================================================== CLI

def run(tmp_path, *args, input_fn=input):
    return _cli(["--journal", str(tmp_path / "j.db"), "--market", str(tmp_path / "m.db"),
                 "--config", str(tmp_path / "none.yaml"), *args], input_fn=input_fn, today=date(2026, 11, 9))


def test_cli_summary_trades_export(tmp_path, capsys):
    seeded(tmp_path, [trade_row(1, 500, "2026-11-04T10:00:00+05:30", True, "A"), trade_row(2, -200, "2026-11-05T10:00:00+05:30", None, "B")])
    assert run(tmp_path, "summary") == 0                                    # default: the 7 days 3-9 Nov
    out = capsys.readouterr().out
    assert "03-Nov-2026 to 09-Nov-2026" in out and "NET P&L       : Rs +300" in out and "Max drawdown  : Rs 200" in out
    assert run(tmp_path, "summary", "--from", "2026-11-05", "--to", "2026-11-05") == 0
    assert "NET P&L       : Rs -200" in capsys.readouterr().out
    assert run(tmp_path, "trades", "--unreviewed") == 0
    listing = capsys.readouterr().out
    assert "#2" in listing and "#1 " not in listing
    assert run(tmp_path, "trades", "--from", "2026-12-01", "--to", "2026-12-02") == 0 and "No trades" in capsys.readouterr().out
    csv_path = tmp_path / "out.csv"
    assert run(tmp_path, "export", "--csv", str(csv_path), "--days", "30") == 0
    rows = list(csv.DictReader(open(csv_path)))
    assert [r["net_pnl"] for r in rows] == ["500.0", "-200.0"] and {"followed_rules", "review_notes"} <= set(rows[0])


def test_cli_review_non_interactive(tmp_path, capsys):
    seeded(tmp_path, [trade_row(1, 100)])
    assert run(tmp_path, "review", "--id", "1", "--followed", "n", "--notes", "chased it", "--by", "uncle") == 0
    t = Journal(tmp_path / "j.db").trades()[0]
    assert (t.followed_rules, t.review_notes, t.reviewed_by) == (False, "chased it", "uncle")
    assert run(tmp_path, "review", "--id", "99", "--followed", "y") == 1
    assert run(tmp_path, "review", "--id", "1") == 2                       # nothing to record
    answers = iter(["y", "", ])
    seeded_again = Journal(tmp_path / "j.db")
    with seeded_again._session() as s:
        s.add(trade_row(2, 5)); s.commit()
    assert run(tmp_path, "review", "--by", "uncle", input_fn=lambda p: next(answers)) == 0
    assert Journal(tmp_path / "j.db").trades(unreviewed_only=True) == []


def test_cli_snapshots_and_prune(tmp_path, capsys):
    store = SnapshotStore(tmp_path / "m.db")
    for d in (1, 9):
        store.write(datetime(2026, 11, d, 10, 0, tzinfo=IST).timestamp(), "NIFTY", "x", 1.0, 1.0, {})
    assert run(tmp_path, "snapshots") == 0
    assert "2 snapshots over 2 day(s)" in capsys.readouterr().out
    assert run(tmp_path, "prune-snapshots", "--keep-days", "3") == 0
    out = capsys.readouterr().out
    assert "Deleted 1" in out and "1 snapshots over 1 day(s)" in out


def test_shipped_journal_config_loads():
    c = load_journal_config("config/journal.yaml")
    assert c.snapshots.interval_seconds == 5 and c.journal_file.endswith("journal.db") and c.market_file != c.journal_file


def test_reused_position_ids_do_not_mix_up_trades(tmp_path):
    """If the portfolio file is lost, ids restart at P1. Each trade must use only ITS OWN fills."""
    j = Journal(tmp_path / "shared.db")
    for n, px in enumerate((100.0, 80.0)):
        w = World(tmp_path / f"w{n}", px={"25000CE": (px, 0.5)})
        w.risk.add_listener(j.on_decision); w.mgr.add_listener(j.on_event)
        assert w.mgr.submit(w.entry(f"e{n}", sl=px * 0.75, target=px * 1.4)).approved
        w.advance(1)
        assert w.mgr.submit(w.exit(f"x{n}")).approved
        w.advance(1)
    a, b = j.trades()
    assert a.position_id == b.position_id == "P1"
    assert a.entry_price == pytest.approx(100.30) and b.entry_price == pytest.approx(80.30)
    assert (a.data_complete, b.data_complete) == (True, True)
    assert a.net_pnl == pytest.approx(-79.0) and b.net_pnl == pytest.approx(-0.6 * 65 - 40)
    assert (a.entry_signal_key, b.entry_signal_key) == ("e0", "e1")
