import sqlite3
from datetime import date, datetime

import pytest

from trader.feed import Instr, Tick
from trader.greeks import IST
from trader.snapshots import SnapshotRecorder, SnapshotStore

IDX, FUT, OPT = Instr("NSE", "99926000"), Instr("NFO", "58875"), Instr("NFO", "40037")
T0 = datetime(2026, 11, 10, 10, 0, tzinfo=IST).timestamp()


def tk(i, ltp, t, bid=None, ask=None, vol=None, oi=None):
    return Tick(i.exchange, i.token, ltp, bid, ask, vol, oi, None, t)


def feed_all(rec, t):
    rec.on_tick(tk(IDX, 25001.0, t)); rec.on_tick(tk(OPT, 100.0, t, 99.75, 100.25, 1200, 5000)); rec.on_tick(tk(FUT, 25031.0, t))


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "market.db")


def recorder(store, interval=5.0):
    return SnapshotRecorder(store, "NIFTY", date(2026, 11, 17), IDX, FUT, interval)


def test_snapshot_contents_roundtrip(store):
    rec = recorder(store)
    feed_all(rec, T0)
    (snap,) = list(store.iter_snapshots())
    assert (snap.underlying, snap.expiry, snap.spot, snap.future, snap.ts) == ("NIFTY", "2026-11-17", 25001.0, 25031.0, T0)
    q = snap.quotes["NFO:40037"]
    assert (q.ltp, q.bid, q.ask, q.volume, q.oi, q.age) == (100.0, 99.75, 100.25, 1200, 5000, 0.0)
    assert "NSE:99926000" not in snap.quotes                       # spot is stored as a field, not a quote
    assert snap.quotes["NFO:58875"].ltp == 25031.0


def test_interval_is_respected_and_stale_quotes_carry_their_age(store):
    rec = recorder(store, 5.0)
    feed_all(rec, T0)
    rec.on_tick(tk(FUT, 25032.0, T0 + 2)); rec.on_tick(tk(FUT, 25033.0, T0 + 4.9))       # too soon
    assert rec.written == 1
    rec.on_tick(tk(FUT, 25034.0, T0 + 5.1))                                                # only the future moved
    snaps = list(store.iter_snapshots())
    assert len(snaps) == 2 and snaps[1].future == 25034.0
    assert snaps[1].quotes["NFO:40037"].age == pytest.approx(5.1, abs=0.05)               # option quote is 5.1 s old: flagged, not hidden


def test_nothing_is_written_until_the_future_has_ticked(store):
    rec = recorder(store)
    rec.on_tick(tk(IDX, 25001.0, T0)); rec.on_tick(tk(OPT, 100.0, T0))
    assert rec.written == 0 and store.stats()["snapshots"] == 0


def test_range_queries_are_ordered_and_half_open(store):
    rec = recorder(store, 1.0)
    for i in range(5):
        feed_all(rec, T0 + i * 10)
    assert [s.ts for s in store.iter_snapshots()] == [T0 + i * 10 for i in range(5)]
    assert [s.ts for s in store.iter_snapshots(T0 + 10, T0 + 30)] == [T0 + 10, T0 + 20]
    assert [s.ts for s in store.iter_snapshots(start_ts=T0 + 30)] == [T0 + 30, T0 + 40]


def test_stats_and_pruning_by_day(store):
    for day, n in ((8, 2), (9, 1), (10, 3)):
        t = datetime(2026, 11, day, 10, 0, tzinfo=IST).timestamp()
        for i in range(n):
            store.write(t + i, "NIFTY", "2026-11-17", 25000.0, 25030.0, {"NFO:1": [1, None, None, None, None, 0.0]})
    st = store.stats()
    assert st["snapshots"] == 6 and st["days"] == ["2026-11-08", "2026-11-09", "2026-11-10"]
    assert store.prune(keep_days=1, today=date(2026, 11, 10)) == 2          # cutoff = 9 Nov: only 8 Nov's two rows go
    assert store.stats()["days"] == ["2026-11-09", "2026-11-10"] and store.stats()["snapshots"] == 4


def test_prune_boundary_exactly(store):
    for day in (7, 8, 9, 10):
        store.write(datetime(2026, 11, day, 10, 0, tzinfo=IST).timestamp(), "NIFTY", "x", 1.0, 1.0, {})
    n = store.prune(keep_days=2, today=date(2026, 11, 10))                  # keep 08, 09, 10; delete 07
    assert n == 1 and store.stats()["days"] == ["2026-11-08", "2026-11-09", "2026-11-10"]


def test_payload_is_compressed(store):
    quotes = {f"NFO:{i}": [100.0 + i, 99.0, 101.0, 1000, 5000, 0.1] for i in range(25)}
    store.write(T0, "NIFTY", "x", 1.0, 1.0, quotes)
    raw = sqlite3.connect(store.path).execute("select length(payload) from snapshots").fetchone()[0]
    import json
    assert raw < len(json.dumps(quotes)) * 0.6


def test_storage_errors_never_reach_the_feed(store, caplog):
    rec = recorder(store)
    store.write = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    feed_all(rec, T0)                                                       # must not raise
    assert rec.written == 0 and "Could not save a market snapshot" in caplog.text


def test_file_from_another_schema_version_is_refused(tmp_path):
    p = tmp_path / "m.db"
    SnapshotStore(p)
    c = sqlite3.connect(p); c.execute("PRAGMA user_version = 99"); c.commit(); c.close()
    with pytest.raises(RuntimeError, match="different version"):
        SnapshotStore(p)
