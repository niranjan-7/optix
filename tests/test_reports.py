import csv
import io
from datetime import date

import pytest

from tests.test_dashboard import Rig, app_for
from tests.test_journal import run, seeded, trade_row
from trader.analytics import TradeStat
from trader.reports import daily_curve, format_monthly, monthly_csv, monthly_report, safe_cell


def T(i, closed, pnl, fees=0.0):
    return TradeStat(i, closed, "s", "st", pnl + fees, fees, 0.0, pnl, None)


# Hand-worked: October  +500, -200, -400 -> net -100, wins 1/3, best 500, worst -400, drawdown: 500,300,-100 -> peak 500 low -100 = 600
#              November +300, +100       -> net +400, wins 2/2, drawdown 0 ; running total -100 then +300
TRADES = [T(1, "2026-10-05T11:00:00+05:30", 500, fees=40), T(2, "2026-10-06T11:00:00+05:30", -200, fees=40),
          T(3, "2026-10-30T11:00:00+05:30", -400, fees=40), T(4, "2026-11-02T11:00:00+05:30", 300, fees=40),
          T(5, "2026-11-02T12:00:00+05:30", 100, fees=40)]


def test_monthly_numbers_match_a_hand_calculation():
    oct_, nov = monthly_report(TRADES)
    assert (oct_.month, oct_.trades, oct_.wins, oct_.losses) == ("2026-10", 3, 1, 2)
    assert oct_.net_pnl == -100 and oct_.fees == 120 and oct_.gross_pnl == 20
    assert (oct_.best_trade, oct_.worst_trade, oct_.max_drawdown, oct_.cumulative_net) == (500, -400, 600, -100)
    assert oct_.win_rate == pytest.approx(1 / 3)
    assert (nov.trades, nov.wins, nov.net_pnl, nov.max_drawdown, nov.cumulative_net, nov.win_rate) == (2, 2, 400, 0, 300, 1.0)


def test_order_of_input_does_not_matter_and_empty_is_empty():
    assert monthly_report(list(reversed(TRADES))) == monthly_report(TRADES)
    assert monthly_report([]) == [] and daily_curve([]) == []


def test_a_month_belongs_to_the_day_the_trade_closed():
    rows = monthly_report([T(1, "2026-10-31T15:20:00+05:30", 10), T(2, "2026-11-01T09:20:00+05:30", 20)])
    assert [(r.month, r.net_pnl) for r in rows] == [("2026-10", 10), ("2026-11", 20)]


def test_daily_curve_is_a_running_total():
    c = daily_curve(TRADES)
    assert [(d["day"], d["net"], d["cumulative"]) for d in c] == [
        ("2026-10-05", 500, 500), ("2026-10-06", -200, 300), ("2026-10-30", -400, -100), ("2026-11-02", 400, 300)]


def test_text_report_shows_totals_and_the_small_sample_caution():
    out = format_monthly(monthly_report(TRADES))
    assert "2026-10" in out and "2026-11" in out and "TOTAL" in out and "CAUTION: only 5 trades" in out
    assert "No closed trades" in format_monthly([])


def test_csv_neutralises_spreadsheet_formulas_but_keeps_numbers_numeric():
    for evil in ("=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\t=1"):
        assert safe_cell(evil).startswith("'")
    assert safe_cell("plain") == "plain" and safe_cell(-12.5) == -12.5 and safe_cell(None) is None
    rows = list(csv.DictReader(io.StringIO(monthly_csv(monthly_report(TRADES)))))
    assert [r["net_pnl"] for r in rows] == ["-100", "400"] or [r["net_pnl"] for r in rows] == ["-100.0", "400.0"]
    assert rows[0]["month"] == "2026-10"


def test_trades_csv_export_is_formula_safe(tmp_path):
    t = trade_row(1, -50)
    t.review_notes = "=cmd|' /C calc'!A0"
    t.entry_reason = "+risky"
    j = seeded(tmp_path, [t])
    out = tmp_path / "t.csv"
    assert j.export_csv(out, None, None) == 1
    row = next(csv.DictReader(open(out)))
    assert row["review_notes"].startswith("'=") and row["entry_reason"] == "'+risky" and row["net_pnl"] == "-50.0"


def test_cli_report_prints_and_writes_csv(tmp_path, capsys):
    seeded(tmp_path, [trade_row(1, 500, "2026-10-05T10:00:00+05:30"), trade_row(2, -200, "2026-11-04T10:00:00+05:30")])
    assert run(tmp_path, "report") == 0                                     # "today" is 2026-11-09 in the helper
    out = capsys.readouterr().out
    assert "MONTHLY P&L REPORT" in out and "2026-10" in out and "2026-11" in out
    assert run(tmp_path, "report", "--months", "1") == 0                    # this month only
    out = capsys.readouterr().out
    assert "2026-11" in out and "2026-10" not in out
    csv_path = tmp_path / "m.csv"
    assert run(tmp_path, "report", "--csv", str(csv_path)) == 0
    assert [r["month"] for r in csv.DictReader(open(csv_path))] == ["2026-10", "2026-11"]
    assert run(tmp_path, "report", "--from", "2027-01-01", "--to", "2027-01-31") == 0
    assert "No closed trades" in capsys.readouterr().out


def test_months_option_crosses_a_year_boundary(tmp_path, capsys):
    seeded(tmp_path, [trade_row(1, 10, "2025-12-10T10:00:00+05:30"), trade_row(2, 20, "2025-11-10T10:00:00+05:30")])
    from trader.journal import _cli
    assert _cli(["--journal", str(tmp_path / "j.db"), "--market", str(tmp_path / "m.db"), "--config", str(tmp_path / "n.yaml"),
                 "report", "--months", "2"], today=date(2026, 1, 15)) == 0
    out = capsys.readouterr().out
    assert "2025-12" in out and "2025-11" not in out                        # Jan 2026 and Dec 2025 only


# ------------------------------------------------------------ dashboard endpoints

@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    with r.j._session() as s:
        s.add_all([trade_row(1, 500, "2026-10-05T10:00:00+05:30"), trade_row(2, -200, "2026-11-10T10:00:00+05:30")])
        s.commit()
    return r


def test_report_endpoint_serves_monthly_curve_and_todays_trades(rig):
    r = app_for(rig).get("/api/report")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    d = r.json()
    assert d["available"] and [m["month"] for m in d["monthly"]] == ["2026-10", "2026-11"]
    assert d["curve"][-1]["cumulative"] == 300
    assert [t["net_pnl"] for t in d["today"]] == [-200]                     # the rig clock is Tue 10 Nov 2026


def test_csv_downloads_are_attachments_and_formula_safe(rig):
    c = app_for(rig)
    r = c.get("/export/trades.csv")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"] and r.text.splitlines()[0].startswith("id,position_id")
    assert len(list(csv.DictReader(io.StringIO(r.text)))) == 2
    m = c.get("/export/monthly.csv")
    assert m.status_code == 200 and m.text.splitlines()[0].startswith("month,trades")
    assert c.get("/export/secrets.csv").status_code == 404


def test_report_routes_obey_the_local_only_rules(rig):
    c = app_for(rig)
    for path in ("/api/report", "/export/trades.csv"):
        assert c.get(path, headers={"host": "evil.example.com"}).status_code == 403        # DNS-rebinding guard
        assert c.get(path, headers={"cf-connecting-ip": "1.2.3.4"}).status_code == 404     # never through the tunnel


def test_report_without_a_journal_is_empty_not_an_error(tmp_path):
    r = Rig(tmp_path)
    r.data._journal = None
    c = app_for(r)
    assert c.get("/api/report").json() == {"monthly": [], "curve": [], "today": [], "available": False}
    assert c.get("/export/trades.csv").status_code == 404


def test_page_has_the_performance_panel_and_download_links(rig):
    html = app_for(rig).get("/").text
    for needle in ("id=\"curve\"", "id=\"monthly\"", "/export/monthly.csv", "/export/trades.csv", "Optix"):
        assert needle in html
