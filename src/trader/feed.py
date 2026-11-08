"""Market-data feed: live Angel One WebSocket + a simulated feed.

Design notes (the "why"):
* Both feeds implement the same `Feed` interface, so strategy/risk code never
  knows which one it is talking to. That lets us develop off-hours.
* `FeedHealth` is the single source of truth for "can we trust prices?".
  The risk engine will reject everything when it is unhealthy (rule: block
  trading on a dead feed). Unhealthy is the default until proven otherwise.
* The subscription set is *desired state* kept by us, not by the SDK. After any
  reconnect we replay it, so a dropped socket can never silently leave us
  watching nothing.
* A failing strategy callback must never kill the socket thread.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import AngelAuth, AuthError
from .instruments import InstrumentMaster

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ models

@dataclass(frozen=True, order=True)
class Instr:
    """An instrument on the feed. Tokens are only unique within an exchange."""
    exchange: str  # "NFO" (options/futures) or "NSE" (spot index)
    token: str

    def __str__(self) -> str:
        return f"{self.exchange}:{self.token}"


@dataclass(frozen=True)
class Tick:
    exchange: str
    token: str
    ltp: float
    bid: float | None
    ask: float | None
    volume: int | None
    oi: int | None
    exchange_ts: float | None  # epoch seconds, from the exchange
    received_at: float  # epoch seconds, when *we* got it

    @property
    def instr(self) -> Instr:
        return Instr(self.exchange, self.token)

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


class FeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in YAML should fail loudly

    version: int = 1
    underlying: str = "NIFTY"
    strikes_each_side: int = Field(5, ge=1, le=25)
    stale_seconds: float = Field(10, gt=0)
    reconnect_after_seconds: float = Field(30, gt=0)
    quote_mode: Literal["ltp", "quote", "snap_quote"] = "snap_quote"

    @model_validator(mode="after")
    def _check(self) -> "FeedConfig":
        if self.reconnect_after_seconds < self.stale_seconds:
            raise ValueError("reconnect_after_seconds must be >= stale_seconds")
        return self


def load_feed_config(path: str | Path = "config/feed.yaml") -> FeedConfig:
    with open(path, encoding="utf-8") as f:
        return FeedConfig(**(yaml.safe_load(f) or {}))


# ------------------------------------------------------------------ health

@dataclass(frozen=True)
class HealthStatus:
    healthy: bool
    connected: bool
    seconds_since_tick: float | None
    seconds_since_activity: float | None  # since last tick OR connect
    reason: str  # human-readable; "ok" when healthy


class FeedHealth:
    """Thread-safe feed health. Unhealthy until fresh ticks are flowing."""

    def __init__(self, stale_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.stale_seconds = stale_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._connected = False
        self._why_down = "feed not started"
        self._connected_at: float | None = None
        self._last_tick: float | None = None
        self._last_by_instr: dict[Instr, float] = {}
        self._critical: set[Instr] = set()

    def set_critical(self, instrs: Iterable[Instr]) -> None:
        """Instruments that must tick regularly (e.g. the index future).

        Far-OTM options can legitimately go quiet, so only these are checked
        individually; everything else is covered by the global rule.
        """
        with self._lock:
            self._critical = set(instrs)

    def set_connected(self, connected: bool, why: str = "") -> None:
        with self._lock:
            if connected and not self._connected:
                self._connected_at = self._clock()
            self._connected = connected
            if not connected:
                self._why_down = why or "disconnected"

    def record_tick(self, instr: Instr) -> None:
        now = self._clock()
        with self._lock:
            self._last_tick = now
            self._last_by_instr[instr] = now

    def status(self) -> HealthStatus:
        now = self._clock()
        with self._lock:
            since_tick = None if self._last_tick is None else now - self._last_tick
            marks = [t for t in (self._last_tick, self._connected_at) if t is not None]
            since_activity = None if not marks else now - max(marks)

            def result(healthy: bool, reason: str) -> HealthStatus:
                return HealthStatus(healthy, self._connected, since_tick, since_activity, reason)

            if not self._connected:
                return result(False, f"feed not connected ({self._why_down})")
            if since_tick is None:
                return result(False, "no ticks received yet")
            # A tick from before the latest (re)connect is not evidence of health.
            if self._connected_at is not None and self._last_tick < self._connected_at:
                return result(False, "no ticks since (re)connect")
            if since_tick > self.stale_seconds:
                return result(False, f"no tick for {since_tick:.1f}s (limit {self.stale_seconds:g}s)")
            for ins in sorted(self._critical):
                t = self._last_by_instr.get(ins)
                if t is None or now - t > self.stale_seconds:
                    return result(False, f"critical instrument {ins} is stale")
            return result(True, "ok")

    def is_healthy(self) -> bool:
        return self.status().healthy


# --------------------------------------------------------------- interface

class Feed(ABC):
    def __init__(self, health: FeedHealth):
        self.health = health
        self._callbacks: list[Callable[[Tick], None]] = []
        self._lock = threading.Lock()  # guards _desired and socket-side bookkeeping
        self._desired: set[Instr] = set()

    def on_tick(self, callback: Callable[[Tick], None]) -> None:
        self._callbacks.append(callback)

    def subscriptions(self) -> frozenset[Instr]:
        with self._lock:
            return frozenset(self._desired)

    def set_subscriptions(self, instrs: Iterable[Instr]) -> None:
        """Declare the full desired set; only the difference is sent."""
        new = set(instrs)
        with self._lock:
            added, removed = new - self._desired, self._desired - new
            self._desired = new
            if added or removed:
                self._apply_subscription_change(added, removed)

    def _apply_subscription_change(self, added: set[Instr], removed: set[Instr]) -> None:
        """Called with self._lock held. Subclasses push the change to the wire."""

    def _emit(self, tick: Tick) -> None:
        self.health.record_tick(tick.instr)
        for cb in list(self._callbacks):
            try:
                cb(tick)
            except Exception:
                # Isolate consumers: one bad callback must not stop the feed.
                log.exception("Tick callback %r failed", getattr(cb, "__name__", cb))

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


# ------------------------------------------------------------ simulated feed

class SimulatedFeed(Feed):
    """Random-walk prices for development and tests (no broker needed).

    NOT a model of real markets — it only exercises the plumbing.
    """

    def __init__(
        self,
        prices: dict[Instr, float],
        health: FeedHealth | None = None,
        seed: int | None = None,
        tick_interval: float = 0.5,
        step_vol: float = 0.0005,
        spread_pct: float = 0.004,
    ):
        super().__init__(health or FeedHealth())
        import random

        self._prices = dict(prices)
        self._rng = random.Random(seed)
        self._interval = tick_interval
        self._vol = step_vol
        self._spread_pct = spread_pct
        self._stop = threading.Event()
        self._paused = False
        self._thread: threading.Thread | None = None

    @staticmethod
    def _round_tick(x: float) -> float:
        return round(max(0.05, round(x / 0.05) * 0.05), 2)

    def step(self, now: float | None = None) -> list[Tick]:
        """Advance every subscribed instrument one step (also used by tests)."""
        if self._paused:
            return []
        now = time.time() if now is None else now
        import math

        out = []
        for ins in sorted(self.subscriptions() & self._prices.keys()):
            p = self._prices[ins] * math.exp(self._rng.gauss(0, self._vol))
            self._prices[ins] = p
            ltp = self._round_tick(p)
            half = max(0.05, ltp * self._spread_pct / 2)
            tick = Tick(ins.exchange, ins.token, ltp, self._round_tick(ltp - half),
                        self._round_tick(ltp + half), None, None, now, now)
            self._emit(tick)
            out.append(tick)
        return out

    def pause(self) -> None:
        """Simulate an outage: no ticks, feed reports disconnected."""
        self._paused = True
        self.health.set_connected(False, "simulated outage")

    def resume(self) -> None:
        self._paused = False
        self.health.set_connected(True)

    def start(self) -> None:
        self._stop.clear()
        self.health.set_connected(True)
        self._thread = threading.Thread(target=self._loop, name="sim-feed", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.step()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.health.set_connected(False, "stopped")


# ----------------------------------------------------------- Angel One feed

_EXCHANGE_TYPE = {"NSE": 1, "NFO": 2}  # SmartWebSocketV2 exchangeType codes
_EXCHANGE_NAME = {1: "NSE", 2: "NFO"}
_MODE = {"ltp": 1, "quote": 2, "snap_quote": 3}


def parse_angel_message(data: dict[str, Any], received_at: float) -> Tick | None:
    """Convert one SmartWebSocketV2 dict into a Tick, or None if unusable.

    Prices arrive in paise (x100). We defensively drop anything odd rather than
    pass garbage to the risk engine.
    """
    try:
        exch = _EXCHANGE_NAME.get(data.get("exchange_type"))
        token = str(data["token"])
        ltp = data["last_traded_price"] / 100.0
    except (KeyError, TypeError):
        return None
    if exch is None or ltp <= 0:
        return None

    def prices(levels: Any) -> list[float]:
        return [lv["price"] / 100.0 for lv in (levels or []) if lv.get("price", 0) > 0]

    buys, sells = prices(data.get("best_5_buy_data")), prices(data.get("best_5_sell_data"))
    bid = max(buys) if buys else None
    ask = min(sells) if sells else None
    # A crossed book means we mislabelled sides or the data is bad. Unknown
    # spread is safe (the risk engine rejects on it); a wrong one is not.
    if bid is not None and ask is not None and bid > ask:
        bid = ask = None

    ts = data.get("exchange_timestamp")
    return Tick(
        exchange=exch, token=token, ltp=ltp, bid=bid, ask=ask,
        volume=data.get("volume_trade_for_the_day"), oi=data.get("open_interest"),
        exchange_ts=ts / 1000.0 if isinstance(ts, (int, float)) and ts > 0 else None,
        received_at=received_at,
    )


def _default_socket_factory(session: Any, settings: Any) -> Any:
    # Lazy import: paper/simulated runs and tests don't need the SDK.
    import logzero
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2

    sws = SmartWebSocketV2(
        session.jwt_token,
        settings.angel_api_key.get_secret_value(),
        settings.angel_client_id,
        session.feed_token,
        max_retry_attempt=0,  # we run our own reconnect loop; don't fight the SDK's
    )
    # The SDK keeps subscription state on the *class*; isolate this instance so a
    # reconnect never replays another socket's stale subscriptions.
    sws.input_request_dict = {}
    # The SDK logs every packet at INFO; that would flood disk.
    logzero.logger.setLevel(logging.WARNING)
    return sws


class AngelFeed(Feed):
    """Live feed with supervised reconnect, resubscribe and a silence watchdog."""

    BACKOFF = (1, 2, 4, 8, 15, 30)  # seconds between reconnect attempts

    def __init__(
        self,
        auth: AngelAuth,
        settings: Any,
        config: FeedConfig,
        health: FeedHealth | None = None,
        socket_factory: Callable[[Any, Any], Any] = _default_socket_factory,
        sleep: Callable[[float], None] | None = None,
    ):
        super().__init__(health or FeedHealth(config.stale_seconds))
        self._auth = auth
        self._settings = settings
        self._cfg = config
        self._factory = socket_factory
        self._stop = threading.Event()
        self._sleep = sleep or (lambda d: self._stop.wait(d))
        self._sock: Any = None
        self._open = False  # True between on_open and disconnect
        self._conn_ticks = 0
        self._threads: list[threading.Thread] = []

    # -- wire helpers (self._lock held by callers)
    def _mode_for(self, exchange: str) -> int:
        # Index tokens only get LTP; depth (bid/ask) is only requested for F&O.
        return 1 if exchange == "NSE" else _MODE[self._cfg.quote_mode]

    def _send(self, sock: Any, action: str, instrs: Iterable[Instr]) -> None:
        by_mode: dict[int, dict[int, list[str]]] = {}
        for i in sorted(instrs):
            by_mode.setdefault(self._mode_for(i.exchange), {}).setdefault(_EXCHANGE_TYPE[i.exchange], []).append(i.token)
        for mode, per_exch in by_mode.items():
            token_list = [{"exchangeType": et, "tokens": toks} for et, toks in per_exch.items()]
            try:
                getattr(sock, action)(uuid.uuid4().hex[:10], mode, token_list)
            except Exception as e:
                # Not fatal: on the next reconnect the full desired set is replayed.
                log.warning("Feed %s failed (%s: %s)", action, type(e).__name__, e)

    def _apply_subscription_change(self, added: set[Instr], removed: set[Instr]) -> None:
        if self._open and self._sock is not None:
            if added:
                self._send(self._sock, "subscribe", added)
            if removed:
                self._send(self._sock, "unsubscribe", removed)

    # -- SDK callbacks
    def _on_open(self, sock: Any) -> None:
        with self._lock:
            self._open = True
            self.health.set_connected(True)
            log.info("Feed connected; subscribing to %d instruments", len(self._desired))
            if self._desired:
                self._send(sock, "subscribe", self._desired)

    def _on_data(self, _ws: Any, data: dict) -> None:
        tick = parse_angel_message(data, time.time())
        if tick is not None:
            self._conn_ticks += 1
            self._emit(tick)

    def _on_error(self, *args: Any) -> None:
        log.warning("Feed socket error: %s", args)

    def _on_close(self, *_: Any) -> None:
        self.health.set_connected(False, "socket closed")

    # -- supervisor
    def _connect_once(self, force_login: bool) -> bool:
        """One connection lifetime. Returns True if any tick arrived on it."""
        self._conn_ticks = 0
        try:
            session = self._auth.login() if force_login else self._auth.ensure_session()
        except AuthError as e:
            log.error("Feed cannot log in: %s", e)
            self.health.set_connected(False, "login failed")
            return False
        except Exception:
            log.exception("Unexpected login error in feed")
            self.health.set_connected(False, "login error")
            return False

        sock = None
        try:
            sock = self._factory(session, self._settings)
            sock.on_open = lambda _ws, s=sock: self._on_open(s)
            sock.on_data = self._on_data
            sock.on_error = self._on_error
            sock.on_close = self._on_close
            with self._lock:
                self._sock = sock
            sock.connect()  # blocks until the socket dies
        except Exception as e:
            log.warning("Feed connection error: %s: %s", type(e).__name__, e)
        finally:
            with self._lock:
                self._sock = None
                self._open = False
            self.health.set_connected(False, "socket closed")
        return self._conn_ticks > 0

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            # After two fruitless attempts, assume the session token may be dead.
            got_ticks = self._connect_once(force_login=failures >= 2)
            failures = 0 if got_ticks else failures + 1
            if self._stop.is_set():
                break
            delay = self.BACKOFF[min(max(failures - 1, 0), len(self.BACKOFF) - 1)]
            log.warning("Feed down; reconnecting in %ss (consecutive failures: %d)", delay, failures)
            self._sleep(delay)

    def _watchdog_check(self) -> bool:
        """Drop a socket that is open but silent (half-dead TCP). True if dropped."""
        st = self.health.status()
        if not st.connected or st.seconds_since_activity is None:
            return False
        if st.seconds_since_activity <= self._cfg.reconnect_after_seconds:
            return False
        with self._lock:
            sock = self._sock
        if sock is None:
            return False
        log.warning("Feed silent for %.0fs while connected; forcing reconnect", st.seconds_since_activity)
        try:
            sock.close_connection()
        except Exception as e:
            log.warning("close_connection failed: %s", e)
        self.health.set_connected(False, "watchdog reconnect")
        return True

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(1.0):
            self._watchdog_check()

    def start(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run, name="angel-feed", daemon=True),
            threading.Thread(target=self._watchdog_loop, name="angel-feed-watchdog", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.close_connection()
            except Exception:
                pass
        for t in self._threads:
            t.join(timeout=5)
        self.health.set_connected(False, "stopped")


# ------------------------------------------------------- ATM window tracking

class ChainTracker:
    """Keeps the feed subscribed to ATM +/- N strikes as the market moves.

    Also always keeps the index future (our underlying) and spot index, plus any
    *pinned* instruments — e.g. open positions, whose quotes we need for stops
    even after the market has moved away from their strike.
    """

    def __init__(self, feed: Feed, master: InstrumentMaster, name: str, expiry: date,
                 strikes_each_side: int, today: date):
        self._feed, self._master = feed, master
        self._name, self._expiry, self._n = name, expiry, strikes_each_side
        self.future = Instr("NFO", master.nearest_future(name, today).token)
        self.index = Instr("NSE", master.index_token(name))
        self._pinned: set[Instr] = set()
        self._atm: float | None = None
        self._window: set[Instr] = set()  # current ATM +/- N contracts
        self._index_seen = False
        self._lock = threading.Lock()
        feed.health.set_critical([self.future])
        feed.on_tick(self._on_tick)
        self._apply()

    @property
    def atm(self) -> float | None:
        return self._atm

    def _apply(self) -> None:
        self._feed.set_subscriptions({self.future, self.index} | self._window | self._pinned)

    def _chain_instrs(self, atm_price: float) -> set[Instr]:
        return {Instr("NFO", o.token) for o in self._master.chain(self._name, self._expiry, atm_price, self._n)}

    def _on_tick(self, tick: Tick) -> None:
        # Prefer spot for ATM; fall back to the future until spot has ticked.
        if tick.instr == self.index:
            self._index_seen = True
        elif tick.instr != self.future or self._index_seen:
            return
        self.recenter(tick.ltp)

    def recenter(self, underlying_price: float) -> bool:
        """Re-pick the window; returns True if the ATM strike changed."""
        with self._lock:
            atm = self._master.atm_strike(self._name, self._expiry, underlying_price)
            if atm == self._atm:
                return False
            self._atm = atm
            log.info("ATM is now %g; updating subscriptions", atm)
            self._window = self._chain_instrs(underlying_price)
            self._apply()
            return True

    def pin(self, instr: Instr) -> None:
        with self._lock:
            self._pinned.add(instr)
            self._apply()

    def unpin(self, instr: Instr) -> None:
        with self._lock:
            self._pinned.discard(instr)
            self._apply()


if __name__ == "__main__":
    # Offline demo (no broker, no credentials):  python -m trader.feed
    # Shows the ATM window rolling as a *simulated* spot moves. Option prices
    # here are crude placeholders — real pricing arrives with the Greeks module.
    from .instruments import ensure_master
    from .logging_utils import setup_logging

    setup_logging()
    cfg = load_feed_config()
    today = date.today()
    master = InstrumentMaster.load(ensure_master(), {cfg.underlying})
    expiry = master.nearest_expiry(cfg.underlying, today)
    spot = 25000.0

    prices: dict[Instr, float] = {}
    fut = master.nearest_future(cfg.underlying, today)
    prices[Instr("NSE", master.index_token(cfg.underlying))] = spot
    prices[Instr("NFO", fut.token)] = spot + 30
    for o in master.chain(cfg.underlying, expiry, spot, 15):
        intrinsic = max(spot - o.strike, 0) if o.opt_type == "CE" else max(o.strike - spot, 0)
        prices[Instr("NFO", o.token)] = intrinsic + 40.0

    feed = SimulatedFeed(prices, health=FeedHealth(cfg.stale_seconds), seed=1, tick_interval=0.2, step_vol=0.0004)
    tracker = ChainTracker(feed, master, cfg.underlying, expiry, cfg.strikes_each_side, today)
    feed.start()
    try:
        for _ in range(12):
            time.sleep(0.5)
            st = feed.health.status()
            print(f"ATM={tracker.atm}  subscribed={len(feed.subscriptions())}  "
                  f"healthy={st.healthy} ({st.reason})")
        feed.pause()
        print("-- simulated outage --")
        print(f"healthy={feed.health.is_healthy()} ({feed.health.status().reason})")
    finally:
        feed.stop()
