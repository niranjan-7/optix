import json
from datetime import date

import pytest

from trader.instruments import (
    InstrumentError, InstrumentMaster, ensure_master, parse_expiry,
)


def row(strike, typ, expiry="09NOV2026", token=None, name="NIFTY", it="OPTIDX"):
    exp_short = expiry[:2] + expiry[2:5] + expiry[7:]
    return {
        "token": token or f"{int(strike)}{typ}{expiry[:2]}", "name": name, "expiry": expiry,
        "symbol": f"{name}{expiry[:5]}{expiry[7:]}{int(strike)}{typ}",
        "strike": f"{strike * 100:.6f}", "lotsize": "65", "instrumenttype": it,
        "exch_seg": "NFO", "tick_size": "5.000000", "freeze_qty": "1801",
    }


def sample_rows():
    rows = []
    for exp in ("09NOV2026", "17NOV2026"):
        for k in range(24800, 25300, 50):
            rows += [row(k, "CE", exp), row(k, "PE", exp)]
    rows.append({"token": "58875", "name": "NIFTY", "expiry": "29DEC2026", "symbol": "NIFTY29DEC26FUT",
                 "strike": "-1.000000", "lotsize": "65", "instrumenttype": "FUTIDX",
                 "exch_seg": "NFO", "tick_size": "10.000000", "freeze_qty": "1801"})
    rows.append({"token": "99926000", "name": "NIFTY", "expiry": "", "symbol": "Nifty 50", "strike": "0",
                 "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0"})
    rows.append(row(25000, "CE", name="BANKNIFTY"))
    return rows


@pytest.fixture
def m():
    return InstrumentMaster.from_rows(sample_rows(), {"NIFTY"})


def test_parse_expiry():
    assert parse_expiry("09NOV2026") == date(2026, 11, 9)
    with pytest.raises(InstrumentError):
        parse_expiry("garbage")


def test_strike_and_tick_scaling(m):
    o = m.find_option("NIFTY", date(2026, 11, 9), 25000, "ce")
    assert o.strike == 25000.0 and o.tick_size == 0.05 and o.lot_size == 65


def test_filters_other_names_and_segments(m):
    assert m.expiries("BANKNIFTY") == []


def test_nearest_expiry_and_skip_today(m):
    assert m.nearest_expiry("NIFTY", date(2026, 11, 8)) == date(2026, 11, 9)
    assert m.nearest_expiry("NIFTY", date(2026, 11, 9)) == date(2026, 11, 9)  # expiry day itself
    assert m.nearest_expiry("NIFTY", date(2026, 11, 9), skip_today=True) == date(2026, 11, 17)
    with pytest.raises(InstrumentError):
        m.nearest_expiry("NIFTY", date(2027, 1, 1))


@pytest.mark.parametrize("spot,atm", [(25012, 25000), (25030, 25050), (25025, 25000), (20000, 24800), (30000, 25250)])
def test_atm(m, spot, atm):
    assert m.atm_strike("NIFTY", date(2026, 11, 9), spot) == atm


def test_strikes_around_atm_and_edges(m):
    e = date(2026, 11, 9)
    assert m.strikes_around_atm("NIFTY", e, 25000, 2) == [24900, 24950, 25000, 25050, 25100]
    assert m.strikes_around_atm("NIFTY", e, 20000, 2) == [24800, 24850, 24900]  # clipped at chain edge


def test_chain_has_ce_and_pe(m):
    c = m.chain("NIFTY", date(2026, 11, 9), 25000, 1)
    assert [(o.strike, o.opt_type) for o in c] == [(24950, "CE"), (24950, "PE"), (25000, "CE"), (25000, "PE"), (25050, "CE"), (25050, "PE")]


def test_missing_option_clear_error(m):
    with pytest.raises(InstrumentError, match="No such option"):
        m.find_option("NIFTY", date(2026, 11, 9), 99999, "CE")


def test_nearest_future(m):
    assert m.nearest_future("NIFTY", date(2026, 11, 8)).symbol == "NIFTY29DEC26FUT"


# ---- download / cache behaviour

def good_fetch(url, dest):
    dest.write_text(json.dumps([{"x": i} for i in range(2000)]))


def test_download_then_cached(tmp_path):
    calls = []
    def fetch(u, d): calls.append(1); good_fetch(u, d)
    p1 = ensure_master(tmp_path, date(2026, 11, 8), fetch=fetch)
    p2 = ensure_master(tmp_path, date(2026, 11, 8), fetch=fetch)
    assert p1 == p2 and len(calls) == 1


def test_truncated_download_falls_back_to_stale_cache(tmp_path):
    old = ensure_master(tmp_path, date(2026, 11, 7), fetch=good_fetch)
    def bad(u, d): d.write_text('[{"x": 1}, {"x"')  # truncated JSON
    got = ensure_master(tmp_path, date(2026, 11, 8), fetch=bad)
    assert got == old
    assert not list(tmp_path.glob("*.part"))  # no leftover partial file
    assert not (tmp_path / "OpenAPIScripMaster-2026-11-08.json").exists()


def test_download_fails_no_cache_raises(tmp_path):
    def boom(u, d): raise OSError("network down")
    with pytest.raises(InstrumentError, match="no cached copy"):
        ensure_master(tmp_path, date(2026, 11, 8), fetch=boom)
