"""Shared test world for execution tests: one clock, fake quotes, real risk/portfolio/paper broker."""
from datetime import datetime

from tests.test_risk import EXP, HEALTHY, TUE, leg, make_cfg  # noqa: F401
from trader.brokers import PaperBroker, PaperConfig
from trader.execution import ExecutionConfig, ExecutionManager
from trader.feed import Tick
from trader.greeks import IST
from trader.portfolio import FeeModel, Portfolio
from trader.risk import RiskEngine, RiskStateStore
from trader.signals import Signal


class World:
    def __init__(self, tmp_path, structures=None, paper=None, risk=None, orders=None, fee=20.0, px=None):
        self.t = TUE.timestamp()
        self.px = {"25000CE": (100.0, 0.5), "25100CE": (60.0, 0.5), "25000PE": (100.0, 0.5)}
        self.px.update(px or {})
        over = dict(risk or {})
        if structures:
            over["structures"] = {"allowed": structures}
        self.cfg = make_cfg(state_file=str(tmp_path / "risk_state.json"), **over)
        self.portfolio = Portfolio(self.quote, FeeModel(brokerage_per_order=fee), tmp_path / "portfolio.json", self.now)
        self.broker = PaperBroker(self.quote, PaperConfig(**(paper or {})), clock=self.epoch)
        self.risk = RiskEngine(self.cfg, self.portfolio, self.quote, lambda: HEALTHY,
                               store=RiskStateStore(self.cfg.state_file), clock=self.now)
        self.econf = ExecutionConfig(paper=PaperConfig(**(paper or {})), orders=orders or {})
        self.mgr = ExecutionManager(self.risk, self.broker, self.portfolio, self.quote, self.econf, clock=self.epoch)
        self.risk.set_actions(self.mgr)
        self.events: list[dict] = []
        self.mgr.add_listener(self.events.append)

    # -- time and quotes
    def now(self): return datetime.fromtimestamp(self.t, IST)
    def epoch(self): return self.t

    def quote(self, token):
        if token not in self.px:
            return None
        price, spread = self.px[token]
        return Tick("NFO", token, price, price - spread / 2, price + spread / 2, None, None, None, self.t)

    def advance(self, seconds, risk_poll=False):
        for _ in range(int(seconds / 0.5)):
            self.t += 0.5
            self.mgr.poll()
            if risk_poll:
                self.risk.poll()

    # -- signals
    def entry(self, key="e1", legs=None, sl=75.2, target=140.0):
        return Signal(key, "ex", "ENTRY", tuple(legs or [leg()]), "test", self.now(), stop_loss=sl, target=target)

    def exit(self, key="x1", pid="P1", legs=None):
        return Signal(key, "ex", "EXIT", tuple(legs or [leg("SELL")]), "test exit", self.now(), position_id=pid)

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]

    def placed(self):
        return [(e["symbol"], e["side"], e["lots"]) for e in self.of("order_placed")]
