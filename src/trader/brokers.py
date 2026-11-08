"""Broker adapters: PaperBroker (default) and AngelBroker (live, double-gated).

Rule 1: real orders require BOTH `LIVE_TRADING=true` in .env AND a person typing
a confirmation at startup. `AngelBroker` cannot even be constructed without the
`LiveGate` object that only `confirm_live_trading()` can issue, and it re-checks
the setting on every order. Nothing in this project turns live mode on for you.

Rule 2: `BrokerAdapter.place_order` is a template method that ALWAYS verifies the
risk engine's seal before calling the adapter-specific `_place`, so a new
adapter cannot forget the check.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import math
import random
import secrets
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from .feed import Tick
from .greeks import IST
from .risk import ApprovedOrder, UnapprovedOrderError, require_approved
from .signals import Leg

log = logging.getLogger(__name__)

TICK = 0.05  # NIFTY option tick size

OPEN, PARTIAL, FILLED, REJECTED, CANCELLED = "OPEN", "PARTIAL", "FILLED", "REJECTED", "CANCELLED"
TERMINAL = {FILLED, REJECTED, CANCELLED}


class BrokerError(RuntimeError):
    """The broker could not do what we asked (message is operator-facing)."""


@dataclass
class BrokerOrder:
    order_id: str
    signal_key: str
    symbol: str
    token: str
    side: str  # BUY | SELL
    lots: int
    lot_size: int
    limit_price: float
    status: str = OPEN
    filled_qty: int = 0  # units, not lots
    avg_price: float = 0.0
    message: str = ""
    placed_at: float = 0.0

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size

    @property
    def filled_lots(self) -> int:
        return self.filled_qty // self.lot_size

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


def round_up(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 2)


def round_down(x: float) -> float:
    return round(math.floor(x / TICK + 1e-9) * TICK, 2)


# ============================================================== interface

class BrokerAdapter(ABC):
    name = "abstract"

    def place_order(self, approved: ApprovedOrder, leg: Leg, lots: int, limit_price: float) -> BrokerOrder:
        """Place ONE leg of an approved signal. Verifies the risk engine's seal first."""
        order = require_approved(approved)
        if leg not in order.signal.legs:
            raise UnapprovedOrderError("this leg is not part of the approved signal")
        if not (isinstance(lots, int) and 1 <= lots <= leg.lots):
            raise ValueError(f"lots must be between 1 and the approved {leg.lots}, got {lots!r}")
        if not math.isfinite(limit_price) or limit_price <= 0:
            raise ValueError(f"limit price must be positive, got {limit_price!r}")
        return self._place(order, leg, lots, round(limit_price, 2))

    @abstractmethod
    def _place(self, approved: ApprovedOrder, leg: Leg, lots: int, limit_price: float) -> BrokerOrder: ...

    @abstractmethod
    def get_order(self, order_id: str) -> BrokerOrder:
        """Current state of an order (a copy; callers cannot mutate broker state)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    def open_orders(self) -> list[BrokerOrder]: ...

    def cancel_all(self) -> int:
        n = 0
        for o in self.open_orders():
            try:
                self.cancel_order(o.order_id)
                n += 1
            except Exception:
                log.exception("Could not cancel order %s", o.order_id)
        return n

    def net_positions(self) -> dict[str, int] | None:
        """token -> net units held at the broker, or None if the adapter cannot say."""
        return None


# ================================================================== paper

class PaperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slippage_ticks: float = Field(1, ge=0)
    latency_seconds: float = Field(0.5, ge=0)
    partial_fill_probability: float = Field(0, ge=0, le=1)
    partial_fill_fraction: float = Field(0.5, gt=0, lt=1)
    reject_probability: float = Field(0, ge=0, le=1)
    seed: int | None = None


@dataclass
class _Sim:
    ready_at: float
    will_reject: bool
    will_partial: bool
    partial_done: bool = False


class PaperBroker(BrokerAdapter):
    """Simulated broker. Fills against the live quote, with slippage and latency.

    This is optimistic compared with reality (no queue, no market impact, no
    outages); treat paper results as an upper bound.
    """
    name = "paper"

    def __init__(self, quotes: Callable[[str], Tick | None], config: PaperConfig | None = None,
                 clock: Callable[[], float] = time.time):
        self._quotes, self._cfg, self._clock = quotes, config or PaperConfig(), clock
        self._rng = random.Random(self._cfg.seed)
        self._orders: dict[str, BrokerOrder] = {}
        self._sim: dict[str, _Sim] = {}
        self._n = 0

    def _place(self, approved: ApprovedOrder, leg: Leg, lots: int, limit_price: float) -> BrokerOrder:
        self._n += 1
        oid = f"PAPER-{self._n:06d}"
        now = self._clock()
        self._orders[oid] = BrokerOrder(oid, approved.signal.idempotency_key, leg.symbol, leg.token, leg.side,
                                        lots, leg.lot_size, limit_price, placed_at=now)
        self._sim[oid] = _Sim(
            ready_at=now + self._cfg.latency_seconds,
            will_reject=self._rng.random() < self._cfg.reject_probability,
            will_partial=lots >= 2 and self._rng.random() < self._cfg.partial_fill_probability,
        )
        return dataclasses.replace(self._orders[oid])

    def _advance(self) -> None:
        now = self._clock()
        for oid, o in self._orders.items():
            sim = self._sim[oid]
            if o.is_terminal or now < sim.ready_at:
                continue
            if sim.will_reject:
                o.status, o.message = REJECTED, "simulated broker rejection"
                continue
            q = self._quotes(o.token)
            if q is None:
                continue
            touch = (q.ask if o.side == "BUY" else q.bid) or q.ltp
            slip = self._cfg.slippage_ticks * TICK
            price = round_up(touch + slip) if o.side == "BUY" else max(TICK, round_down(touch - slip))
            # Marketable-limit semantics: refuse to trade through our limit; try again later.
            if (o.side == "BUY" and price > o.limit_price) or (o.side == "SELL" and price < o.limit_price):
                continue
            remaining_lots = (o.qty - o.filled_qty) // o.lot_size
            fill_lots = remaining_lots
            if sim.will_partial and not sim.partial_done and remaining_lots >= 2:
                fill_lots = max(1, int(remaining_lots * self._cfg.partial_fill_fraction))
                sim.partial_done = True
                sim.ready_at = now + self._cfg.latency_seconds  # the rest arrives later
            fq = fill_lots * o.lot_size
            o.avg_price = (o.avg_price * o.filled_qty + price * fq) / (o.filled_qty + fq)
            o.filled_qty += fq
            o.status = FILLED if o.filled_qty >= o.qty else PARTIAL

    def get_order(self, order_id: str) -> BrokerOrder:
        self._advance()
        try:
            return dataclasses.replace(self._orders[order_id])
        except KeyError:
            raise BrokerError(f"unknown order {order_id}") from None

    def cancel_order(self, order_id: str) -> None:
        self._advance()
        o = self._orders.get(order_id)
        if o is None:
            raise BrokerError(f"unknown order {order_id}")
        if not o.is_terminal:
            o.status = CANCELLED  # keeps whatever was already filled

    def open_orders(self) -> list[BrokerOrder]:
        self._advance()
        return [dataclasses.replace(o) for o in self._orders.values() if not o.is_terminal]


# =================================================================== live

class LiveTradingNotConfirmed(RuntimeError):
    """Live mode was requested but the safeguards were not satisfied."""


_GATE_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class LiveGate:
    """Proof that a person confirmed live trading today. Issued only by confirm_live_trading()."""
    client_id: str
    day: str
    proof: str

    def valid_for(self, client_id: str, now: datetime | None = None) -> bool:
        today = (now or datetime.now(IST)).astimezone(IST).date().isoformat()
        expect = hmac.new(_GATE_KEY, f"{client_id}|{self.day}".encode(), hashlib.sha256).hexdigest()
        return self.client_id == client_id and self.day == today and hmac.compare_digest(self.proof, expect)


LIVE_WARNING = """
==========================  LIVE TRADING  ==========================
You are about to allow REAL ORDERS with REAL MONEY on account {client}.
Have you completed the go-live checklist in the README?
  - paper results reviewed for 4+ weeks   - risk limits set
  - static IP whitelisted with Angel One  - minimum lot size only
The kill switch is:  python -m trader.risk kill
=====================================================================
"""


def confirm_live_trading(settings: Any, input_fn: Callable[[str], str] | None = None,
                         now: datetime | None = None) -> LiveGate:
    """Second key for live mode. Requires LIVE_TRADING=true AND a typed phrase.

    Refuses outright when nobody is at a keyboard (e.g. a restart loop in Docker):
    live mode must never resume unattended.
    """
    if not settings.live_trading:
        raise LiveTradingNotConfirmed("LIVE_TRADING is not 'true' in .env; staying in paper mode")
    if input_fn is None:
        if not sys.stdin or not sys.stdin.isatty():
            raise LiveTradingNotConfirmed(
                "live mode needs a person at the keyboard to confirm; refusing to start it unattended")
        input_fn = input
    print(LIVE_WARNING.format(client=settings.angel_client_id))
    phrase = f"START LIVE {settings.angel_client_id}"
    if input_fn(f"To continue, type exactly:  {phrase}\n> ").strip() != phrase:
        raise LiveTradingNotConfirmed("confirmation phrase did not match; live mode NOT enabled")
    day = (now or datetime.now(IST)).astimezone(IST).date().isoformat()
    proof = hmac.new(_GATE_KEY, f"{settings.angel_client_id}|{day}".encode(), hashlib.sha256).hexdigest()
    log.critical("LIVE TRADING CONFIRMED by operator for client %s", settings.angel_client_id)
    return LiveGate(settings.angel_client_id, day, proof)


class AngelBroker(BrokerAdapter):
    """Live Angel One adapter.

    *** UNTESTED AGAINST THE REAL BROKER. *** Field names follow Angel One's
    published order-book fields (orderstatus, filledshares, unfilledshares,
    averageprice) but must be verified with a 1-lot test as part of the go-live
    checklist. Known API constraints handled here: the order book is rate
    limited (about 1 request/second) so it is cached; a placeOrder reply can be
    lost while the order is live, so we reconcile against the book before
    deciding an order failed.
    """
    name = "angel"

    def __init__(self, auth: Any, settings: Any, gate: LiveGate, product_type: str = "INTRADAY",
                 clock: Callable[[], float] = time.time, min_book_interval: float = 1.1):
        if not isinstance(gate, LiveGate) or not gate.valid_for(settings.angel_client_id):
            raise LiveTradingNotConfirmed("AngelBroker needs a valid LiveGate from confirm_live_trading()")
        self._auth, self._settings, self._gate = auth, settings, gate
        self._product, self._clock, self._min_interval = product_type, clock, min_book_interval
        self._orders: dict[str, BrokerOrder] = {}
        self._book_at = 0.0
        self._last_rows: list[dict] = []

    # -- guards
    def _assert_live(self) -> None:
        if not self._settings.live_trading or not self._gate.valid_for(self._settings.angel_client_id):
            raise LiveTradingNotConfirmed("live trading is no longer confirmed for today; refusing to send")

    def _client(self) -> Any:
        self._auth.ensure_session()
        return self._auth.client

    # -- placing
    def _place(self, approved: ApprovedOrder, leg: Leg, lots: int, limit_price: float) -> BrokerOrder:
        self._assert_live()
        qty = lots * leg.lot_size
        params = {
            "variety": "NORMAL", "tradingsymbol": leg.symbol, "symboltoken": leg.token,
            "transactiontype": leg.side, "exchange": "NFO", "ordertype": "LIMIT",
            "producttype": self._product, "duration": "DAY", "price": f"{limit_price:.2f}",
            "squareoff": "0", "stoploss": "0", "quantity": str(qty),
        }
        now = self._clock()
        client = self._client()
        order_id: str | None = None
        try:
            order_id = client.placeOrder(dict(params))
        except Exception as e:
            log.error("placeOrder raised %s: %s", type(e).__name__, e)
        if not order_id:
            # The reply may have been lost while the order is live. Look before giving up,
            # otherwise a retry could double the position.
            order_id = self._find_in_book(leg, qty, limit_price, now)
        if not order_id:
            o = BrokerOrder("NONE", approved.signal.idempotency_key, leg.symbol, leg.token, leg.side, lots,
                            leg.lot_size, limit_price, REJECTED, message="broker returned no order id", placed_at=now)
            return o
        o = BrokerOrder(str(order_id), approved.signal.idempotency_key, leg.symbol, leg.token, leg.side, lots,
                        leg.lot_size, limit_price, placed_at=now)
        self._orders[o.order_id] = o
        return dataclasses.replace(o)

    def _find_in_book(self, leg: Leg, qty: int, price: float, since: float) -> str | None:
        rows = self._fetch_book(force=True)
        for r in rows:
            if (r.get("tradingsymbol") == leg.symbol and r.get("transactiontype") == leg.side
                    and str(r.get("quantity")) == str(qty) and r.get("orderid") not in self._orders):
                try:
                    if abs(float(r.get("price", 0)) - price) < 0.011:
                        log.warning("Adopted order %s found in the book after a lost placeOrder reply", r["orderid"])
                        return str(r["orderid"])
                except (TypeError, ValueError):
                    continue
        return None

    # -- reading the book (rate limited, cached)
    def _fetch_book(self, force: bool = False) -> list[dict]:
        now = self._clock()
        if not force and now - self._book_at < self._min_interval:
            return self._last_rows
        resp = self._client().orderBook()
        self._book_at = now
        rows = (resp or {}).get("data") or []
        self._last_rows = rows
        return rows

    def _refresh(self) -> None:
        rows = {str(r.get("orderid")): r for r in self._fetch_book()}
        for oid, o in self._orders.items():
            if o.is_terminal or oid not in rows:
                continue
            r = rows[oid]
            status = str(r.get("orderstatus") or r.get("status") or "").lower()
            try:
                filled = int(float(r.get("filledshares") or 0))
                avg = float(r.get("averageprice") or 0)
            except (TypeError, ValueError):
                log.error("Unparseable fill data for order %s: %s", oid, r)
                continue
            o.filled_qty, o.avg_price, o.message = filled, avg, str(r.get("text") or "")
            if status == "complete" or (filled >= o.qty > 0):
                o.status = FILLED
            elif status == "rejected":
                o.status = REJECTED
            elif status in ("cancelled", "canceled"):
                o.status = CANCELLED
            elif filled > 0:
                o.status = PARTIAL
            else:
                o.status = OPEN
                if status not in ("open", "open pending", "validation pending", "put order req received",
                                  "trigger pending", "modified", "modify pending", "pending", ""):
                    log.warning("Unrecognised Angel order status %r for %s; treating as open", status, oid)

    def get_order(self, order_id: str) -> BrokerOrder:
        self._refresh()
        try:
            return dataclasses.replace(self._orders[order_id])
        except KeyError:
            raise BrokerError(f"unknown order {order_id}") from None

    def cancel_order(self, order_id: str) -> None:
        self._assert_live()
        resp = self._client().cancelOrder(order_id, "NORMAL")
        if not resp or not resp.get("status", False):
            raise BrokerError(f"cancel request for {order_id} failed: {resp}")

    def open_orders(self) -> list[BrokerOrder]:
        self._refresh()
        return [dataclasses.replace(o) for o in self._orders.values() if not o.is_terminal]

    def net_positions(self) -> dict[str, int] | None:
        resp = self._client().position()
        out: dict[str, int] = {}
        for r in (resp or {}).get("data") or []:
            try:
                qty = int(float(r.get("netqty") or 0))
            except (TypeError, ValueError):
                continue
            if qty:
                out[str(r.get("symboltoken"))] = qty
        return out


def build_broker(settings: Any, auth: Any, quotes: Callable[[str], Tick | None], paper_config: PaperConfig,
                 product_type: str = "INTRADAY", input_fn: Callable[[str], str] | None = None) -> BrokerAdapter:
    """Paper unless live mode is both configured AND confirmed by a person."""
    if not settings.live_trading:
        log.info("PAPER MODE: no real orders will be sent")
        return PaperBroker(quotes, paper_config)
    gate = confirm_live_trading(settings, input_fn)  # raises if not confirmed -> startup aborts
    return AngelBroker(auth, settings, gate, product_type)
