import dataclasses
import json
import logging
from datetime import date, datetime, timedelta

import pytest
import yaml

from tests.test_strategy import Harness
from trader.feed import HealthStatus, Instr, Tick
from trader.greeks import IST
from trader.risk import (
    ApprovedOrder, Code, RiskConfig, RiskEngine, RiskStateStore, UnapprovedOrderError, _cli,
    load_risk_config, require_approved,
)
from trader.signals import Leg, PositionView, Signal

TUE = datetime(2026, 11, 10, 10, 30, tzinfo=IST)  # a Tuesday, mid-morning
EXP = date(2026, 11, 17)
HEALTHY = HealthStatus(True, True, 0.1, 0.1, "ok")
SICK = HealthStatus(False, False, None, None, "feed not connected (test)")
ALL_STRUCTURES = ["LONG_OPTION", "VERTICAL_DEBIT", "VERTICAL_CREDIT", "IRON_CONDOR"]


def make_cfg(**over) -> RiskConfig:
    raw = yaml.safe_load(open("config/risk.yaml"))

    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                merge(a[k], v)
            else:
                a[k] = v
    merge(raw, over)
    return RiskConfig(**raw)


class Clock:
    def __init__(self, now=TUE): self.now = now
    def __call__(self): return self.now


class Portfolio:
    def __init__(self): self.positions, self.realised, self.unrealised = [], 0.0, 0.0
    def open_positions(self): return list(self.positions)
    def realised_pnl_today(self): return self.realised
    def unrealised_pnl(self): return self.unrealised


class Actions:
    def __init__(self, engine_ref=None):
        self.calls, self.fail, self.engine = [], False, engine_ref
    def cancel_all_pending(self):
        if self.fail: raise RuntimeError("broker unreachable")
        self.calls.append("cancel")
    def square_off_all(self):
        if self.fail: raise RuntimeError("broker unreachable")
        self.calls.append("square_off")
        if self.engine:  # like the real execution module: close via EXIT signals through the engine
            for i, p in enumerate(self.engine._portfolio.open_positions()):
                sig = Signal(f"kill-exit-{p.position_id}-{len(self.calls)}", p.strategy, "EXIT",
                             tuple(l.opposite() for l in p.legs), "kill", self.engine._clock(), position_id=p.position_id)
                self.results.append(self.engine.evaluate(sig))
    results: list = []


def tick(price, spread=0.5, age=0.0, now=TUE, bid=True):
    b = price - spread / 2 if bid else None
    return Tick("NFO", "x", price, b, price + spread / 2, None, None, None, now.timestamp() - age)


def leg(side="BUY", strike=25000, typ="CE", lots=1, exp=EXP):
    return Leg("NIFTY", exp, strike, typ, side, lots, 65, f"NIFTY{strike}{typ}", f"{strike}{typ}")


class Rig:
    def __init__(self, tmp_path, with_actions=True, **cfg_over):
        self.cfg = make_cfg(state_file=str(tmp_path / "risk_state.json"), **cfg_over)
        self.clock, self.portfolio = Clock(), Portfolio()
        self.quotes: dict[str, Tick] = {"25000CE": tick(100.0), "25000PE": tick(100.0)}
        self.health = HEALTHY
        self.store = RiskStateStore(self.cfg.state_file)
        self.notes: list[tuple[str, str]] = []
        self.actions = Actions() if with_actions else None
        self.engine = self._engine()
        if self.actions:
            self.actions.engine = self.engine
            self.actions.results = []

    def _engine(self):
        return RiskEngine(self.cfg, self.portfolio, lambda t: self.quotes.get(t), lambda: self.health,
                          store=self.store, actions=self.actions, notifier=lambda l, m: self.notes.append((l, m)),
                          clock=self.clock)

    def restart(self):
        self.engine = self._engine()
        if self.actions:
            self.actions.engine = self.engine
        return self.engine

    def entry(self, key="k1", legs=None, sl=75.2, target=140.0, **kw):
        return Signal(key, "ex", "ENTRY", tuple(legs or [leg()]), "test", self.clock(), stop_loss=sl, target=target, **kw)

    def position(self, pid="P1", legs=None):
        p = PositionView(pid, "ex", tuple(legs or [leg()]), 100.25, 75.2, 140.0, TUE)
        self.portfolio.positions.append(p)
        return p

    def exit(self, key="x1", pid="P1", legs=None):
        legs = legs if legs is not None else [leg("SELL")]
        return Signal(key, "ex", "EXIT", tuple(legs), "test exit", self.clock(), position_id=pid)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def codes(d):
    return set(d.codes)


# ================================================================ happy path

def test_valid_entry_is_approved_with_sealed_order(rig):
    d = rig.engine.evaluate(rig.entry())
    assert d.approved and d.reasons == () and d.summary() == "approved"
    o = d.order
    assert isinstance(o, ApprovedOrder) and o.verify() and require_approved(o) is o
    a = o.assessment
    assert a.structure == "LONG_OPTION"
    assert a.loss_at_stop == pytest.approx((100.25 - 75.2) * 65)  # pays the ask
    assert a.max_loss == pytest.approx(100.25 * 65)
    assert rig.engine.status()["orders_used"] == 1


# ================================================================= rejections

def test_kill_switch_blocks_entries(rig):
    rig.engine.kill("testing")
    assert codes(rig.engine.evaluate(rig.entry())) == {Code.KILL_SWITCH}


def test_unhealthy_feed_blocks_entries(rig):
    rig.health = SICK
    d = rig.engine.evaluate(rig.entry())
    assert codes(d) == {Code.FEED_UNHEALTHY} and "not connected" in d.summary()


@pytest.mark.parametrize("when", [
    datetime(2026, 11, 14, 10, 30, tzinfo=IST),   # Saturday
    datetime(2026, 11, 10, 9, 19, tzinfo=IST),    # before 09:20
    datetime(2026, 11, 10, 15, 1, tzinfo=IST),    # after no_new_entries_after 15:00
])
def test_entry_outside_window(rig, when):
    rig.clock.now = when
    rig.quotes["25000CE"] = tick(100.0, now=when)
    assert codes(rig.engine.evaluate(rig.entry())) == {Code.OUTSIDE_WINDOW}


def test_window_edges_are_inclusive(rig):
    for h, m in ((9, 20), (15, 0)):
        rig.clock.now = datetime(2026, 11, 10, h, m, tzinfo=IST)
        rig.quotes["25000CE"] = tick(100.0, now=rig.clock.now)
        assert rig.engine.evaluate(rig.entry(key=f"k{h}")).approved


def test_duplicate_key_rejected_but_rejection_does_not_consume_key(rig):
    rig.health = SICK
    assert not rig.engine.evaluate(rig.entry("same")).approved   # rejected: key must stay free
    rig.health = HEALTHY
    assert rig.engine.evaluate(rig.entry("same")).approved
    rig.engine.entry_settled("same")
    assert codes(rig.engine.evaluate(rig.entry("same"))) == {Code.DUPLICATE}


def test_naked_short_always_rejected_even_if_everything_enabled(tmp_path):
    r = Rig(tmp_path, structures={"allowed": ALL_STRUCTURES})
    r.quotes["25000CE"] = tick(100.0)
    d = r.engine.evaluate(r.entry(legs=[leg("SELL")], sl=130.0, target=60.0))
    assert Code.NOT_DEFINED_RISK in codes(d) and "naked" in d.summary()
    # ...including a short straddle and an uncovered ratio
    d = r.engine.evaluate(r.entry("k2", legs=[leg("SELL"), leg("SELL", typ="PE")], sl=130.0))
    assert Code.NOT_DEFINED_RISK in codes(d)
    d = r.engine.evaluate(r.entry("k3", legs=[leg("BUY"), leg("SELL", 25100, lots=2)], sl=30.0))
    assert Code.NOT_DEFINED_RISK in codes(d)


def test_recognised_but_disabled_structure(rig):
    rig.quotes["25100CE"] = tick(60.0)
    d = rig.engine.evaluate(rig.entry(legs=[leg("BUY"), leg("SELL", 25100)], sl=20.0))
    assert Code.STRUCTURE_DISABLED in codes(d)


def test_credit_spread_and_iron_condor_work_when_enabled(tmp_path):
    r = Rig(tmp_path, structures={"allowed": ALL_STRUCTURES})
    r.quotes.update({"25000CE": tick(110.0), "25100CE": tick(60.0)})
    d = r.engine.evaluate(r.entry("cs", legs=[leg("SELL"), leg("BUY", 25100)], sl=80.0, target=10.0))
    assert d.approved and d.order.assessment.structure == "VERTICAL_CREDIT"
    assert d.order.assessment.max_loss == pytest.approx((100 - 49.5) * 65)   # width - credit
    assert d.order.assessment.loss_at_stop == pytest.approx((80 - 49.5) * 65)
    r.engine.entry_settled("cs")

    r2 = Rig(tmp_path / "b", structures={"allowed": ALL_STRUCTURES})
    r2.quotes.update({"24800PE": tick(10.0, 0.2), "24900PE": tick(40.0, 0.2), "25100CE": tick(45.0, 0.2), "25200CE": tick(15.0, 0.2)})
    ic = [leg("BUY", 24800, "PE"), leg("SELL", 24900, "PE"), leg("SELL", 25100), leg("BUY", 25200)]
    d = r2.engine.evaluate(r2.entry("ic", legs=ic, sl=80.0, target=20.0))
    assert d.approved and d.order.assessment.structure == "IRON_CONDOR"
    assert r2.engine.status()["orders_used"] == 4   # counts legs


def test_stop_loss_is_mandatory(rig):
    d = rig.engine.evaluate(rig.entry(sl=None))
    assert codes(d) == {Code.NO_STOP_LOSS}


@pytest.mark.parametrize("sl,tgt", [(100.25, 140.0), (120.0, None), (0.0, None), (-5.0, None), (float("nan"), None), (75.0, 90.0)])
def test_bad_stop_or_target(rig, sl, tgt):
    assert Code.BAD_STOP_OR_TARGET in codes(rig.engine.evaluate(rig.entry(sl=sl, target=tgt)))


@pytest.mark.parametrize("q,code", [
    (tick(100.0, spread=5.0), Code.SPREAD_TOO_WIDE),          # 5%
    (tick(1000.0, spread=2.5), Code.SPREAD_TOO_WIDE),         # tiny %, but > Rs2 absolute
    (tick(100.0, bid=False), Code.SPREAD_UNKNOWN),
    (tick(100.0, age=30), Code.STALE_QUOTE),
    (None, Code.NO_QUOTE),
])
def test_quote_problems(rig, q, code):
    if q is None:
        rig.quotes.clear()
    else:
        rig.quotes["25000CE"] = q
    assert code in codes(rig.engine.evaluate(rig.entry()))


def test_lot_caps_including_expiry_day(tmp_path):
    r = Rig(tmp_path)
    assert Code.TOO_MANY_LOTS in codes(r.engine.evaluate(r.entry(legs=[leg(lots=2)])))

    r = Rig(tmp_path / "e", limits={"max_lots_per_order": 3}, expiry_day={"max_lots_per_order": 1})
    r.clock.now = datetime(2026, 11, 17, 10, 30, tzinfo=IST)   # expiry day
    r.quotes["25000CE"] = tick(100.0, now=r.clock.now)
    d = r.engine.evaluate(r.entry(legs=[leg(lots=2)], sl=75.2))
    assert Code.TOO_MANY_LOTS in codes(d) and "expiry-day cap" in d.summary()
    r.clock.now = datetime(2026, 11, 10, 10, 30, tzinfo=IST)   # normal day, same size is fine
    r.quotes["25000CE"] = tick(100.0, now=r.clock.now)
    assert r.engine.evaluate(r.entry("k2", legs=[leg(lots=2)], sl=90.0, target=130.0)).approved is False  # loss limit, not lots
    assert Code.TOO_MANY_LOTS not in codes(r.engine.evaluate(r.entry("k3", legs=[leg(lots=2)], sl=90.0)))


def test_expiry_day_entries_can_be_blocked(tmp_path):
    r = Rig(tmp_path, expiry_day={"block_new_entries": True})
    r.clock.now = datetime(2026, 11, 17, 10, 30, tzinfo=IST)
    r.quotes["25000CE"] = tick(100.0, now=r.clock.now)
    assert codes(r.engine.evaluate(r.entry())) == {Code.EXPIRY_DAY_BLOCKED}


def test_max_open_positions_counts_portfolio_and_in_flight(tmp_path):
    r = Rig(tmp_path, limits={"max_open_positions": 1})
    assert r.engine.evaluate(r.entry("a")).approved            # approved, fill not yet known
    d = r.engine.evaluate(r.entry("b"))                         # burst of signals must not slip past the limit
    assert codes(d) == {Code.MAX_POSITIONS}
    r.engine.entry_settled("a")                                 # execution reports back: no position resulted
    assert r.engine.evaluate(r.entry("c")).approved
    r.engine.entry_settled("c"); r.position()                   # now a real position exists
    assert codes(r.engine.evaluate(r.entry("d"))) == {Code.MAX_POSITIONS}


def test_in_flight_entry_expires_after_ttl(tmp_path):
    r = Rig(tmp_path, limits={"max_open_positions": 1}, inflight={"pending_ttl_seconds": 60})
    r.engine.evaluate(r.entry("a"))
    r.clock.now = TUE + timedelta(seconds=61)
    r.quotes["25000CE"] = tick(100.0, now=r.clock.now)
    assert r.engine.evaluate(r.entry("b")).approved


def test_max_orders_per_day_and_reset_next_day(tmp_path):
    r = Rig(tmp_path, limits={"max_orders_per_day": 2, "max_open_positions": 10})
    assert r.engine.evaluate(r.entry("a")).approved and r.engine.evaluate(r.entry("b")).approved
    assert codes(r.engine.evaluate(r.entry("c"))) == {Code.MAX_ORDERS}
    r.clock.now = datetime(2026, 11, 11, 10, 30, tzinfo=IST)   # next day
    r.quotes["25000CE"] = tick(100.0, now=r.clock.now)
    assert r.engine.evaluate(r.entry("c")).approved


def test_loss_limits(rig):
    d = rig.engine.evaluate(rig.entry(sl=40.0))           # (100.25-40)*65 = 3916 > 2000
    assert codes(d) == {Code.MAX_LOSS_PER_TRADE}
    rig.quotes["25000CE"] = tick(200.0)                   # structural loss 13,016 > 10,000
    d = rig.engine.evaluate(rig.entry("k2", sl=190.0, target=260.0))
    assert codes(d) == {Code.MAX_STRUCTURE_LOSS}


def test_all_reasons_are_reported_together(rig):
    rig.engine.kill("t")
    rig.health = SICK
    d = rig.engine.evaluate(rig.entry(sl=None))
    assert {Code.KILL_SWITCH, Code.FEED_UNHEALTHY, Code.NO_STOP_LOSS} <= codes(d)


def test_every_rejection_is_logged_with_reason(rig, caplog):
    rig.health = SICK
    with caplog.at_level(logging.INFO, logger="trader.risk"):
        rig.engine.evaluate(rig.entry("logme"))
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "REJECTED" in msg and "logme" in msg and "FEED_UNHEALTHY" in msg and "not connected" in msg


def test_listeners_see_every_decision_and_cannot_break_the_engine(rig):
    seen = []
    rig.engine.add_listener(lambda s, d: (_ for _ in ()).throw(RuntimeError("journal down")))
    rig.engine.add_listener(lambda s, d: seen.append((s.idempotency_key, d.approved)))
    rig.engine.evaluate(rig.entry("ok"))
    rig.health = SICK
    rig.engine.evaluate(rig.entry("bad"))
    assert seen == [("ok", True), ("bad", False)]


# ====================================================================== exits

def test_exit_of_held_position_is_approved_even_when_everything_else_blocks(rig):
    rig.position()
    rig.engine.kill("t")
    rig.health = SICK
    rig.clock.now = datetime(2026, 11, 14, 3, 0, tzinfo=IST)     # Saturday 3am
    d = rig.engine.evaluate(rig.exit())
    assert d.approved and d.order.verify()


def test_exit_that_would_open_a_position_is_rejected(rig):
    # Mislabelled "exit" with nothing to close = a naked short in disguise.
    d = rig.engine.evaluate(rig.exit(pid=None))
    assert codes(d) == {Code.EXIT_NOT_COVERED}
    rig.position()
    assert Code.EXIT_NOT_COVERED in codes(rig.engine.evaluate(rig.exit("x2", legs=[leg("SELL", lots=2)])))       # too big
    assert Code.EXIT_NOT_COVERED in codes(rig.engine.evaluate(rig.exit("x3", legs=[leg("SELL", 25100)])))        # other contract
    assert Code.EXIT_NOT_COVERED in codes(rig.engine.evaluate(rig.exit("x4", legs=[leg("BUY")])))                # would enlarge the long
    assert Code.EXIT_NOT_COVERED in codes(rig.engine.evaluate(rig.exit("x5", legs=[leg("SELL"), leg("SELL")]))) # double-closing


def test_exit_of_unknown_position(rig):
    rig.position()
    assert Code.UNKNOWN_POSITION in codes(rig.engine.evaluate(rig.exit(pid="P9")))


def test_exit_in_flight_blocks_repeat_until_settled_or_expired(rig):
    rig.position()
    assert rig.engine.evaluate(rig.exit("x1")).approved
    assert codes(rig.engine.evaluate(rig.exit("x2"))) == {Code.EXIT_IN_FLIGHT}
    rig.engine.exit_settled("P1")                                  # execution says the first one failed
    assert rig.engine.evaluate(rig.exit("x3")).approved
    rig.clock.now = TUE + timedelta(seconds=61)
    assert rig.engine.evaluate(rig.exit("x4")).approved            # TTL expiry also allows a retry


def test_exit_closing_a_credit_spread(tmp_path):
    r = Rig(tmp_path, structures={"allowed": ALL_STRUCTURES})
    r.position(legs=[leg("SELL"), leg("BUY", 25100)])
    d = r.engine.evaluate(r.exit(legs=[leg("BUY"), leg("SELL", 25100)]))
    assert d.approved
    r2 = Rig(tmp_path / "b")
    r2.position(legs=[leg("SELL"), leg("BUY", 25100)])
    # selling the long wing first (leaving the short naked) is NOT a valid exit
    assert Code.EXIT_NOT_COVERED in codes(r2.engine.evaluate(r2.exit(legs=[leg("SELL", 25100)])))


# ============================================================== fail closed

def test_internal_errors_reject(rig):
    rig.portfolio.open_positions = lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
    d = rig.engine.evaluate(rig.entry())
    assert not d.approved and codes(d) == {Code.INTERNAL_ERROR}
    rig.portfolio.open_positions = lambda: []
    rig.quotes = None  # lookups now raise TypeError
    assert codes(rig.engine.evaluate(rig.entry("k2"))) == {Code.INTERNAL_ERROR}


def test_corrupt_state_file_forces_kill_switch_on(rig):
    rig.engine.evaluate(rig.entry("a"))
    open(rig.cfg.state_file, "w").write("{ this is not json")
    d = rig.engine.evaluate(rig.entry("b"))
    assert codes(d) == {Code.KILL_SWITCH} and "unreadable" in d.summary()
    assert list(rig.store.path.parent.glob("risk_state.json.corrupt.*"))     # evidence kept
    assert rig.engine.status()["kill_active"]
    rig.engine.reset_kill("uncle", "inspected the file, safe")
    assert rig.engine.evaluate(rig.entry("c")).approved


def test_missing_state_file_is_a_normal_first_run(rig):
    assert not rig.store.path.exists()
    assert rig.engine.status()["kill_active"] is False


# ============================================================ kill switch

def test_kill_cancels_then_squares_off_and_exits_pass_through_the_engine(rig):
    rig.position()
    rig.engine.kill("fat finger")
    assert rig.actions.calls == ["cancel", "square_off"]
    assert [r.approved for r in rig.actions.results] == [True]    # the square-off exit was approved despite the kill
    rig.engine.poll()
    assert rig.actions.calls == ["cancel", "square_off"]          # not repeated
    assert ("CRITICAL", "KILL SWITCH ON by operator: fat finger") in rig.notes


def test_kill_persists_across_restart_until_manual_reset(rig):
    rig.engine.kill("t")
    e2 = rig.restart()
    assert codes(e2.evaluate(rig.entry())) == {Code.KILL_SWITCH}
    for op, note in (("", "x"), ("uncle", " ")):
        with pytest.raises(ValueError):
            e2.reset_kill(op, note)
    e2.reset_kill("uncle", "positions checked, feed healthy")
    assert e2.evaluate(rig.entry("again")).approved
    assert any("RESET" in m for _, m in rig.notes)


def test_failed_kill_actions_are_retried_and_alerted(rig):
    rig.actions.fail = True
    rig.engine.kill("t")                                            # must not raise
    assert any(l == "CRITICAL" and "FAILED" in m for l, m in rig.notes)
    assert codes(rig.engine.evaluate(rig.entry())) == {Code.KILL_SWITCH}   # still blocked
    rig.actions.fail = False
    rig.engine.poll()
    assert rig.actions.calls == ["cancel", "square_off"]
    rig.engine.poll()
    assert rig.actions.calls == ["cancel", "square_off"]


def test_kill_from_another_process_takes_effect_immediately_and_is_actioned_by_poll(rig):
    assert _cli(["--state", rig.cfg.state_file, "kill", "--reason", "from phone"]) == 0
    assert codes(rig.engine.evaluate(rig.entry())) == {Code.KILL_SWITCH}   # no restart, no poll needed
    rig.engine.poll()
    assert rig.actions.calls == ["cancel", "square_off"]


def test_cli_reset_needs_confirmation(rig, monkeypatch, capsys):
    rig.engine.kill("t")
    st = rig.cfg.state_file
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert _cli(["--state", st, "reset", "--operator", "uncle", "--note", "ok"]) == 1
    assert rig.engine.status()["kill_active"]
    monkeypatch.setattr("builtins.input", lambda _: "RESET")
    assert _cli(["--state", st, "reset", "--operator", "uncle", "--note", "ok"]) == 0
    assert not rig.engine.status()["kill_active"]
    assert _cli(["--state", st, "status"]) == 0
    assert json.loads(capsys.readouterr().out.split("Kill switch reset by uncle.\n")[-1])["kill_active"] is False


# ============================================================ daily loss halt

@pytest.mark.parametrize("realised,unrealised,halted", [
    (-1000, -2999, False), (-1000, -3000, True), (-4500, 0, True), (-2000, 500, False), (1000, -5000, True),
])
def test_daily_loss_boundary_uses_realised_plus_unrealised(rig, realised, unrealised, halted):
    rig.portfolio.realised, rig.portfolio.unrealised = realised, unrealised
    d = rig.engine.evaluate(rig.entry())
    assert (Code.DAY_HALTED in codes(d)) is halted


def test_halt_squares_off_once_blocks_all_day_and_lifts_next_day(rig):
    rig.position()
    rig.portfolio.unrealised = -4100
    rig.engine.poll()                                               # trips without any signal arriving
    assert rig.actions.calls == ["cancel", "square_off"]
    rig.engine.poll()
    assert rig.actions.calls == ["cancel", "square_off"]
    assert any("Daily loss limit" in m for _, m in rig.notes)
    rig.portfolio.unrealised = 0                                    # even if P&L recovers, stay halted today
    assert Code.DAY_HALTED in codes(rig.restart().evaluate(rig.entry()))
    rig.clock.now = datetime(2026, 11, 11, 10, 30, tzinfo=IST)
    rig.quotes["25000CE"] = tick(100.0, now=rig.clock.now)
    assert rig.engine.evaluate(rig.entry("tomorrow")).approved


def test_halt_without_square_off_option(tmp_path):
    r = Rig(tmp_path, limits={"square_off_on_daily_loss": False})
    r.portfolio.realised = -9999
    r.engine.poll()
    assert r.actions.calls == []
    assert Code.DAY_HALTED in codes(r.engine.evaluate(r.entry()))


# ====================================================== forced end-of-day

def test_force_square_off_at_configured_time_once(rig):
    rig.position()
    rig.clock.now = datetime(2026, 11, 10, 15, 19, tzinfo=IST)
    rig.engine.poll()
    assert rig.actions.calls == []
    rig.clock.now = datetime(2026, 11, 10, 15, 20, tzinfo=IST)
    rig.engine.poll(); rig.engine.poll()
    assert rig.actions.calls == ["square_off"]                      # no order cancels, once only


def test_no_forced_square_off_when_flat_or_weekend(rig):
    rig.clock.now = datetime(2026, 11, 10, 15, 25, tzinfo=IST)
    rig.engine.poll()
    assert rig.actions.calls == []
    rig.position()
    rig.clock.now = datetime(2026, 11, 14, 15, 25, tzinfo=IST)      # Saturday
    rig.engine.poll()
    assert rig.actions.calls == []


# ======================================================= seal / bypass guard

def test_broker_gate_rejects_anything_not_approved_by_the_engine(rig):
    d = rig.engine.evaluate(rig.entry())
    good = d.order
    sig = rig.entry("other")
    forged = ApprovedOrder("id", sig, TUE, good.assessment, "0" * 64)
    tampered = dataclasses.replace(good, signal=sig)
    for bad in (forged, tampered, sig, None, "order", {"signal": sig}):
        with pytest.raises(UnapprovedOrderError):
            require_approved(bad)
    assert require_approved(good) is good


def test_rejected_decisions_carry_no_order(rig):
    d = rig.engine.evaluate(rig.entry(sl=None))
    assert d.order is None and not d.approved


# ============================================================== persistence

def test_idempotency_and_order_counts_survive_restart(rig):
    assert rig.engine.evaluate(rig.entry("k")).approved
    e2 = rig.restart()
    assert codes(e2.evaluate(rig.entry("k"))) == {Code.DUPLICATE}
    assert e2.status()["orders_used"] == 1


# ============================================ strategy -> risk integration

def test_example_strategy_signal_passes_the_real_risk_engine(tmp_path):
    h = Harness()
    h.trigger_up()
    sig = h.signals[0]
    cfg = make_cfg(state_file=str(tmp_path / "s.json"))
    engine = RiskEngine(cfg, Portfolio(), lambda tok: h.ctx.latest(Instr("NFO", tok)), lambda: HEALTHY,
                        clock=lambda: sig.created_at)
    d = engine.evaluate(sig)
    assert d.approved, d.summary()
    assert d.order.assessment.loss_at_stop == pytest.approx((sig.ref_price - sig.stop_loss) * 65)
    assert d.order.assessment.loss_at_stop < cfg.limits.max_loss_per_trade


# ==================================================================== config

def test_shipped_risk_yaml_loads_with_agreed_limits():
    c = load_risk_config("config/risk.yaml")
    assert (c.limits.max_loss_per_trade, c.limits.max_loss_per_day) == (2000, 4000)
    assert c.structures.allowed == ["LONG_OPTION"] and c.limits.square_off_on_daily_loss is True


@pytest.mark.parametrize("path,value", [
    (("window", "no_new_entries_after"), 1500),                 # unquoted-style number
    (("window", "trading_start"), "15:10"),                    # not before the other times
    (("window", "force_square_off"), "15:00"),
    (("window", "weekdays"), ["Mon", "Funday"]),
    (("structures", "allowed"), ["LONG_OPTION", "NAKED_SHORT"]),
    (("limits", "max_loss_per_trade"), 9000),                  # exceeds per-day limit
    (("limits", "max_loss_per_day"), -1),
    (("quotes", "typo_field"), 1),
])
def test_bad_risk_config_is_refused(path, value):
    raw = yaml.safe_load(open("config/risk.yaml"))
    raw[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        RiskConfig(**raw)
