from datetime import date, datetime

import pytest

from tests.test_instruments import sample_rows
from trader.example_momentum import ExampleMomentum, ExampleMomentumParams
from trader.feed import Instr, Tick
from trader.greeks import IST
from trader.instruments import InstrumentMaster
from trader.signals import Leg, Signal
from trader.strategy import (
    InMemoryPortfolio, LiveContext, Strategy, StrategyConfig, StrategyConfigError, StrategyRunner,
    build_strategies, load_strategy_config,
)

DAY = date(2026, 11, 10)  # a Tuesday; the 09-Nov expiry has passed, 17-Nov is next
EXPIRY = date(2026, 11, 17)
FUT, IDX = Instr("NFO", "58875"), Instr("NSE", "99926000")
INDEX_ROW = {"token": "99926000", "name": "NIFTY", "expiry": "", "symbol": "Nifty 50", "strike": "0",
             "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0"}


def ts(h, m, s=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST).timestamp()


class Harness:
    def __init__(self, expiry=EXPIRY, **params):
        master = InstrumentMaster.from_rows(sample_rows() + [INDEX_ROW], {"NIFTY"})
        self.master = master
        self.portfolio = InMemoryPortfolio()
        self.ctx = LiveContext(master, "NIFTY", expiry, FUT, IDX, self.portfolio)
        self.strat = ExampleMomentum("ex", ExampleMomentumParams(**params))
        self.signals: list[Signal] = []
        self.runner = StrategyRunner([self.strat], self.ctx, self.signals.append)

    def tick(self, instr, price, t, half_spread=0.25):
        self.runner.on_tick(Tick(instr.exchange, instr.token, price, price - half_spread, price + half_spread,
                                 None, None, t, t))

    def chain(self, t, ce=100.0, pe=100.0):
        for o in self.master._options:
            if o.expiry == EXPIRY:
                self.tick(Instr("NFO", o.token), ce if o.opt_type == "CE" else pe, t)

    def market(self, spot, t, ce=100.0, pe=100.0):
        """One 'moment': refresh option quotes, index, then the future last."""
        self.chain(t, ce, pe)
        self.tick(IDX, spot, t)
        self.tick(FUT, spot + 10, t)  # future trades at a premium; irrelevant to the rule

    def trigger_up(self, start=25000.0, jump=35.0, t0=ts(9, 40), dt=300):
        self.market(start, t0)
        self.market(start + jump, t0 + dt)


# ------------------------------------------------------------------ entries

def test_up_move_buys_atm_call_with_sl_and_target():
    h = Harness(); h.trigger_up()
    assert len(h.signals) == 1
    s = h.signals[0]
    leg = s.legs[0]
    assert (s.kind, s.strategy, leg.side, leg.opt_type, leg.strike, leg.lots, leg.lot_size) == \
           ("ENTRY", "ex", "BUY", "CE", 25050, 1, 65)  # spot 25035 -> ATM 25050
    assert s.ref_price == pytest.approx(100.25)  # paid the ask, pessimistically
    assert s.stop_loss == pytest.approx(75.20) and s.target == pytest.approx(140.35)
    assert "+35.0 pts" in s.reason and s.created_at.tzinfo is not None
    assert s.idempotency_key == "ex:ENTRY:" + leg.symbol + ":202611100945"


def test_down_move_buys_atm_put():
    h = Harness(); h.trigger_up(jump=-40.0)
    assert [(s.legs[0].opt_type, s.legs[0].strike) for s in h.signals] == [("PE", 24950)]


def test_small_move_no_signal():
    h = Harness(); h.trigger_up(jump=29.0)
    assert h.signals == []


def test_needs_warmup_history():
    h = Harness()
    h.market(25000, ts(9, 40)); h.market(25040, ts(9, 41))  # big move but only 1 min of history
    assert h.signals == []


def test_data_gap_is_not_a_price_move():
    h = Harness()
    h.market(25000, ts(9, 31)); h.market(25060, ts(9, 50))  # 19 minutes apart (feed outage)
    assert h.signals == []


@pytest.mark.parametrize("start,end", [((9, 20), (9, 25)), ((14, 28), (14, 33))])
def test_no_entry_outside_entry_window(start, end):
    h = Harness()
    h.market(25000, ts(*start)); h.market(25040, ts(*end))
    assert h.signals == []


def test_max_entries_per_day():
    h = Harness(); h.portfolio._entries[("ex", DAY)] = 2
    h.trigger_up()
    assert h.signals == []


def test_no_new_entry_while_position_open():
    h = Harness(); h.trigger_up()
    h.portfolio.apply(h.signals[0], 100.25)
    h.market(25100, ts(9, 52), ce=104)  # a further move
    assert [s.kind for s in h.signals] == ["ENTRY"]


def test_cooldown_after_signal_even_if_risk_rejected():
    h = Harness(cooldown_minutes=5, max_entries_per_day=5)
    h.trigger_up()  # signal 1 at 09:45; portfolio never told => as if risk rejected it
    h.market(25080, ts(9, 46))  # still trending, 1 min later
    h.market(25100, ts(9, 49))  # 4 min after the signal: trend intact, but inside the cooldown
    assert len(h.signals) == 1
    h.market(25120, ts(9, 52))  # 7 min after: cooldown over, trend continues
    assert len(h.signals) == 2 and h.signals[1].idempotency_key != h.signals[0].idempotency_key


def test_reversal_after_cooldown_gives_opposite_side():
    h = Harness(cooldown_minutes=5, max_entries_per_day=5)
    h.trigger_up()
    h.market(25080, ts(9, 46)); h.market(25000, ts(9, 52))  # trend reverses hard
    assert [s.legs[0].opt_type for s in h.signals] == ["CE", "PE"]


def test_no_entry_on_expiry_day_when_configured():
    day = EXPIRY
    h = Harness(expiry=EXPIRY)
    h.market(25000, ts(9, 40, day=day)); h.market(25040, ts(9, 45, day=day))
    assert h.signals == []
    h2 = Harness(expiry=EXPIRY, skip_expiry_day=False)
    h2.market(25000, ts(9, 40, day=day)); h2.market(25040, ts(9, 45, day=day))
    assert len(h2.signals) == 1


def test_stale_or_missing_option_quote_blocks_entry():
    h = Harness()
    h.tick(IDX, 25000, ts(9, 40)); h.tick(FUT, 25010, ts(9, 40))
    h.tick(IDX, 25040, ts(9, 45)); h.tick(FUT, 25050, ts(9, 45))  # no option quotes at all
    assert h.signals == []
    h.chain(ts(9, 45) - 30)  # quotes exist but 30s old
    h.tick(FUT, 25055, ts(9, 45, 1))
    assert h.signals == []


def test_history_resets_each_day():
    h = Harness()
    h.market(25000, ts(15, 0, day=DAY))
    nxt = date(2026, 11, 11)
    h.chain(ts(9, 30, 5, day=nxt)); h.tick(IDX, 25040, ts(9, 30, 5, day=nxt)); h.tick(FUT, 25050, ts(9, 30, 5, day=nxt))
    assert h.signals == []  # yesterday's close is not a reference for today


# -------------------------------------------------------------------- exits

def open_position(h):
    h.trigger_up()
    h.portfolio.apply(h.signals[0], 100.25)
    return h.signals[0].legs[0]


def test_stop_loss_exit_closes_opposite_leg():
    h = Harness(); leg = open_position(h)
    h.tick(Instr("NFO", leg.token), 80.0, ts(9, 50)); assert len(h.signals) == 1  # above SL
    h.tick(Instr("NFO", leg.token), 75.0, ts(9, 51))
    s = h.signals[1]
    assert s.kind == "EXIT" and s.position_id == "P1" and "stop-loss" in s.reason
    assert s.legs[0].side == "SELL" and s.legs[0].token == leg.token


def test_target_exit():
    h = Harness(); leg = open_position(h)
    h.tick(Instr("NFO", leg.token), 141.0, ts(10, 5))
    assert h.signals[1].kind == "EXIT" and "target" in h.signals[1].reason


def test_square_off_exit_on_any_tick():
    h = Harness(); open_position(h)
    h.tick(IDX, 25040, ts(15, 14, 59)); assert len(h.signals) == 1
    h.tick(IDX, 25040, ts(15, 15, 0))
    assert h.signals[1].kind == "EXIT" and "square-off" in h.signals[1].reason


def test_exit_signals_are_rate_limited_but_retry():
    h = Harness(exit_retry_seconds=5); leg = open_position(h)
    o = Instr("NFO", leg.token)
    h.tick(o, 70, ts(9, 50, 0)); h.tick(o, 69, ts(9, 50, 2))
    assert [s.kind for s in h.signals] == ["ENTRY", "EXIT"]  # 2nd tick suppressed
    h.tick(o, 68, ts(9, 50, 6))  # position still open after 6s => ask again
    assert [s.kind for s in h.signals] == ["ENTRY", "EXIT", "EXIT"]
    assert h.signals[1].idempotency_key != h.signals[2].idempotency_key


def test_exit_takes_priority_over_new_entry():
    h = Harness(); leg = open_position(h)
    h.tick(Instr("NFO", leg.token), 70, ts(9, 50))
    assert h.signals[-1].kind == "EXIT"


# ------------------------------------------------------------- runner/safety

class Boom(Strategy):
    type_name = "boom"
    def on_tick(self, tick, ctx): raise RuntimeError("bug")


class Echo(Strategy):
    type_name = "echo"
    def on_tick(self, tick, ctx):
        leg = Leg("NIFTY", EXPIRY, 25000, "CE", "BUY", 1, 65, "S", "T")
        return Signal("k", self.name, "ENTRY", (leg,), "r", datetime.now(IST))


def tk():
    return Tick("NFO", "1", 1.0, None, None, None, None, 0.0, 0.0)


def test_crashing_strategy_is_isolated_then_disabled():
    h = Harness()
    disabled = []
    r = StrategyRunner([Boom("boom", None), Echo("echo", None)], h.ctx, h.signals.append, on_disabled=disabled.append)
    for _ in range(7):
        r.on_tick(tk())
    assert "boom" in r.disabled and [s.name for s in disabled] == ["boom"]
    assert len(h.signals) == 7  # the healthy strategy was never affected


def test_submit_failure_does_not_propagate():
    h = Harness()
    def bad_submit(sig): raise RuntimeError("risk engine down")
    StrategyRunner([Echo("echo", None)], h.ctx, bad_submit).on_tick(tk())  # must not raise


# ------------------------------------------------------------ signals/config

def leg_kwargs(**kw):
    base = dict(underlying="NIFTY", expiry=EXPIRY, strike=25000, opt_type="CE", side="BUY",
                lots=1, lot_size=65, symbol="S", token="T")
    base.update(kw); return base


@pytest.mark.parametrize("bad", [dict(opt_type="XX"), dict(side="LONG"), dict(lots=0), dict(lots=1.5),
                                 dict(lot_size=0), dict(strike=0)])
def test_leg_validation(bad):
    with pytest.raises(ValueError):
        Leg(**leg_kwargs(**bad))


def test_signal_validation_and_missing_sl_is_allowed():
    leg = Leg(**leg_kwargs())
    now = datetime.now(IST)
    s = Signal("k", "s", "ENTRY", [leg], "r", now)  # no stop-loss: the RISK ENGINE rejects that, not us
    assert s.legs == (leg,) and s.stop_loss is None and leg.qty == 65
    for kw in (dict(kind="BUY"), dict(legs=()), dict(idempotency_key=""), dict(created_at=datetime.now())):
        args = dict(idempotency_key="k", strategy="s", kind="ENTRY", legs=(leg,), reason="r", created_at=now)
        args.update(kw)
        with pytest.raises(ValueError):
            Signal(**args)
    assert leg.opposite().side == "SELL" and leg.opposite().opposite() == leg


def test_shipped_strategy_yaml_builds_example():
    strategies = build_strategies(load_strategy_config("config/strategy.yaml"))
    assert [s.name for s in strategies] == ["example_momentum"]
    assert strategies[0].params.stop_loss_pct == 25 and strategies[0].params.square_off.hour == 15


def write_cfg(tmp_path, text):
    p = tmp_path / "s.yaml"; p.write_text(text); return load_strategy_config(p)


def test_disabled_strategy_not_built(tmp_path):
    cfg = write_cfg(tmp_path, "strategies:\n  - {name: a, type: example_momentum, enabled: false}\n")
    assert build_strategies(cfg) == []


def test_unknown_type_and_bad_params_give_clear_errors(tmp_path):
    with pytest.raises(StrategyConfigError, match="unknown type 'nope'"):
        build_strategies(write_cfg(tmp_path, "strategies:\n  - {name: a, type: nope}\n"))
    with pytest.raises(StrategyConfigError, match="bad params"):
        build_strategies(write_cfg(tmp_path, "strategies:\n  - {name: a, type: example_momentum, params: {typo_pts: 3}}\n"))
    with pytest.raises(StrategyConfigError, match="bad params"):
        build_strategies(write_cfg(tmp_path, "strategies:\n  - {name: a, type: example_momentum, params: {stop_loss_pct: 150}}\n"))


def test_unquoted_yaml_time_is_rejected_not_misread(tmp_path):
    text = "strategies:\n  - name: a\n    type: example_momentum\n    params:\n      entry_end: 14:30\n"
    with pytest.raises(StrategyConfigError, match="quoted"):
        build_strategies(write_cfg(tmp_path, text))


def test_time_order_enforced(tmp_path):
    text = 'strategies:\n  - {name: a, type: example_momentum, params: {entry_end: "15:30"}}\n'
    with pytest.raises(StrategyConfigError, match="entry_start < entry_end < square_off"):
        build_strategies(write_cfg(tmp_path, text))


def test_duplicate_names_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        StrategyConfig(strategies=[dict(name="a", type="x"), dict(name="a", type="y")])
