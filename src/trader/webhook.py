"""Webhook receiver: POST /webhook turns an external alert (e.g. TradingView) into a Signal.

Rule 2: the alert becomes a `Signal` and is handed to `submit` - in production
`ExecutionManager.submit`, which runs it through the risk engine first. This
module never touches a broker and has no way to skip the risk engine.

The endpoint is reachable from the internet (through a tunnel), so it is
defensive by default:
  * a shared secret is mandatory (the app refuses to start without a strong one);
    optional HMAC signature on top
  * strict schema: unknown fields, wrong types, NaN/Infinity, lowercase/odd
    actions are all refused with a precise message
  * body size cap, global rate limit, and a lockout for repeated wrong secrets
  * a request can only ever BUY (open a long option) or EXIT (close one);
    selling to open is not expressible
  * every other route (dashboard, kill switch) is invisible to tunnel traffic
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from .config import Settings, require_webhook_secret
from .feed import Tick
from .greeks import IST
from .instruments import InstrumentError, InstrumentMaster
from .ratelimit import TokenBucket  # noqa: F401  (re-exported)
from .risk import Decision
from .signals import Leg, Signal

log = logging.getLogger(__name__)

PUBLIC_PATHS = {"/webhook", "/health"}  # everything else is local-only (see guard below)
TUNNEL_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-visitor", "x-forwarded-for")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# ================================================================== config

class RateCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_minute: float = Field(20, gt=0)
    burst: int = Field(5, ge=1)


class AuthFailCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_per_minute: int = Field(5, ge=1)
    block_minutes: float = Field(10, gt=0)


class WebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    allowed_symbols: list[str] = ["NIFTY"]
    skip_expiry_day: bool = True
    max_body_bytes: int = Field(4096, ge=256, le=65536)
    max_age_seconds: float = Field(120, gt=0)
    allow_body_secret: bool = True
    # Believe Cloudflare's client-IP header, but only on loopback connections (the tunnel
    # runs on this machine). The origin must never be exposed directly - see docs/webhook.md.
    trust_proxy_headers: bool = True
    # Which direct peers may vouch for Cloudflare's client-IP header. "*" means any peer: only safe when the
    # port is reachable from this computer alone (docker-compose.yml publishes it to 127.0.0.1 only).
    trusted_proxy_peers: list[str] = ["127.0.0.1", "::1", "localhost"]
    rate_limit: RateCfg = RateCfg()
    auth_failures: AuthFailCfg = AuthFailCfg()


def load_webhook_config(path: str | Path = "config/webhook.yaml") -> WebhookConfig:
    with open(path, encoding="utf-8") as f:
        return WebhookConfig(**(yaml.safe_load(f) or {}))


# ============================================================== rate limits

class AuthFailureGuard:
    """Blocks an address after repeated wrong secrets (slows down guessing)."""

    def __init__(self, max_per_minute: int, block_minutes: float, clock: Callable[[], float] = time.monotonic):
        self._max, self._block, self._clock = max_per_minute, block_minutes * 60, clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked_for(self, ip: str) -> float:
        with self._lock:
            until = self._blocked_until.get(ip)
            if until is None:
                return 0.0
            left = until - self._clock()
            if left <= 0:
                del self._blocked_until[ip]
                return 0.0
            return left

    def record_failure(self, ip: str) -> bool:
        """Returns True if this failure triggered a block."""
        with self._lock:
            now = self._clock()
            q = self._fails[ip]
            q.append(now)
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self._max:
                self._blocked_until[ip] = now + self._block
                q.clear()
                return True
            return False


# ================================================================== payload

class WebhookSignal(BaseModel):
    """The alert message. Anything not listed here is rejected."""
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    idempotency_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    symbol: str = Field(min_length=1, max_length=20)
    action: Literal["BUY", "EXIT"]
    strike: float = Field(gt=0, allow_inf_nan=False)
    option_type: Literal["CE", "PE"]
    qty: int = Field(gt=0, le=100_000)  # UNITS, a multiple of the lot size (NIFTY: 65)
    stop_loss: float | None = Field(None, gt=0, allow_inf_nan=False)  # absolute option price
    target: float | None = Field(None, gt=0, allow_inf_nan=False)
    # Alternative to the absolute levels, for senders (TradingView) that cannot do arithmetic on
    # the option price: "25" = 25% below / above the entry price, taken from the live quote.
    stop_loss_pct: float | None = Field(None, gt=0, lt=100, allow_inf_nan=False)
    target_pct: float | None = Field(None, gt=0, le=1000, allow_inf_nan=False)
    expiry: date | None = None
    timestamp: datetime | None = None

    @model_validator(mode="after")
    def _exit_has_no_levels(self) -> "WebhookSignal":
        levels = (self.stop_loss, self.target, self.stop_loss_pct, self.target_pct)
        if self.action == "EXIT" and any(v is not None for v in levels):
            raise ValueError("stop-loss/target only apply to BUY; an EXIT just closes the position")
        if self.stop_loss is not None and self.stop_loss_pct is not None:
            raise ValueError("give stop_loss OR stop_loss_pct, not both")
        if self.target is not None and self.target_pct is not None:
            raise ValueError("give target OR target_pct, not both")
        return self


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _friendly_errors(e: ValidationError) -> list[dict[str, str]]:
    """Field + message only. Never echo the submitted value (it might be a secret)."""
    out = []
    for err in e.errors():
        field = ".".join(str(p) for p in err["loc"]) or "(body)"
        msg = err["msg"]
        if err["type"] == "literal_error" and field == "action":
            msg = ("action must be exactly \"BUY\" (open a long option) or \"EXIT\" (close it). "
                   "Selling to open is not supported. Check the capitalisation.")
        elif err["type"] == "extra_forbidden":
            msg = "unknown field"
        out.append({"field": field, "message": msg})
    return out


def _tick_round(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


def to_signal(p: WebhookSignal, master: InstrumentMaster, cfg: WebhookConfig, now: datetime,
              quotes: Callable[[str], Tick | None] | None = None) -> Signal:
    """Resolve the alert to a concrete contract. Raises ValueError with an operator-friendly message."""
    sym = p.symbol.upper()
    if sym not in cfg.allowed_symbols:
        raise ValueError(f"symbol {p.symbol!r} is not allowed (allowed: {cfg.allowed_symbols})")
    try:
        expiry = p.expiry or master.nearest_expiry(sym, now.date(), skip_today=cfg.skip_expiry_day)
        opt = master.find_option(sym, expiry, p.strike, p.option_type)
    except InstrumentError as e:
        raise ValueError(str(e)) from e
    if p.qty % opt.lot_size != 0:
        raise ValueError(f"qty is in UNITS and must be a multiple of the lot size: {opt.lot_size} = 1 lot "
                         f"(you sent {p.qty}; did you mean {opt.lot_size}?)")
    lots = p.qty // opt.lot_size
    stop_loss, target = p.stop_loss, p.target
    if p.stop_loss_pct is not None or p.target_pct is not None:
        q = quotes(opt.token) if quotes else None
        if q is None:
            raise ValueError("no live quote for this contract, so a percentage stop/target cannot be turned into "
                             "a price; send absolute stop_loss/target instead")
        entry = q.ask or q.ltp  # the price we expect to pay, same basis as the risk engine
        if p.stop_loss_pct is not None:
            stop_loss = _tick_round(entry * (1 - p.stop_loss_pct / 100))
        if p.target_pct is not None:
            target = _tick_round(entry * (1 + p.target_pct / 100))
    leg = Leg(sym, expiry, opt.strike, opt.opt_type, "BUY" if p.action == "BUY" else "SELL", lots, opt.lot_size,
              opt.symbol, opt.token)
    return Signal(
        idempotency_key=f"webhook:{p.idempotency_id}", strategy="webhook",
        kind="ENTRY" if p.action == "BUY" else "EXIT", legs=(leg,),
        reason=f"webhook {p.action} {opt.symbol} x{lots} lot(s)",
        created_at=now, stop_loss=stop_loss, target=target, source="webhook")


# ==================================================================== app

def create_app(
    settings: Settings,
    master: InstrumentMaster,
    submit: Callable[[Signal], Decision],
    config: WebhookConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    quotes: Callable[[str], Tick | None] | None = None,
) -> FastAPI:
    """Build the app. `submit` must be ExecutionManager.submit (it applies the risk engine)."""
    require_webhook_secret(settings)  # raises ConfigError -> the app will not start
    cfg = config or WebhookConfig()
    clock = clock or (lambda: datetime.now(IST))
    secret = settings.webhook_secret.get_secret_value().encode()
    hmac_secret = settings.webhook_hmac_secret.get_secret_value().encode()
    bucket = TokenBucket(cfg.rate_limit.per_minute, cfg.rate_limit.burst, monotonic)
    guard = AuthFailureGuard(cfg.auth_failures.max_per_minute, cfg.auth_failures.block_minutes, monotonic)

    app = FastAPI(title="options-trading", docs_url=None, redoc_url=None, openapi_url=None)  # no public API docs

    def error(status: int, detail: Any, headers: dict[str, str] | None = None) -> JSONResponse:
        return JSONResponse({"status": "error", "detail": detail}, status_code=status, headers=headers)

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        cf = request.headers.get("cf-connecting-ip")
        if cfg.trust_proxy_headers and cf and ("*" in cfg.trusted_proxy_peers or peer in cfg.trusted_proxy_peers):
            try:
                return str(ipaddress.ip_address(cf.strip()))
            except ValueError:
                pass
        return peer

    # -- local-only guard: anything that arrived through the tunnel may only reach PUBLIC_PATHS.
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.url.path not in PUBLIC_PATHS and any(h in request.headers for h in TUNNEL_HEADERS):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}  # deliberately says nothing else to the outside world

    async def read_body(request: Request) -> bytes | None:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cfg.max_body_bytes:
            return None
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > cfg.max_body_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def authentic(request: Request, raw: bytes, body_secret: Any) -> bool:
        header = request.headers.get("x-webhook-secret")
        supplied = header if header is not None else (body_secret if cfg.allow_body_secret else None)
        ok = isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), secret)
        if hmac_secret:  # evaluated regardless of `ok`, so timing doesn't reveal which check failed
            want = "sha256=" + hmac.new(hmac_secret, raw, hashlib.sha256).hexdigest()
            sig_ok = hmac.compare_digest(request.headers.get("x-signature", "").encode(), want.encode())
            ok = ok and sig_ok
        return ok

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        ip = client_ip(request)

        left = guard.blocked_for(ip)
        if left > 0:
            return error(429, "too many failed attempts; try again later", {"Retry-After": str(math.ceil(left))})
        wait = bucket.take()
        if wait > 0:
            return error(429, "rate limit exceeded", {"Retry-After": str(math.ceil(wait))})

        raw = await read_body(request)
        if raw is None:
            return error(413, f"body larger than {cfg.max_body_bytes} bytes")
        try:
            data = json.loads(raw, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError):
            return error(400, "body is not valid JSON")
        if not isinstance(data, dict):
            return error(422, "body must be a JSON object")

        body_secret = data.pop("secret", None)
        if not authentic(request, raw, body_secret):
            blocked = guard.record_failure(ip)
            log.warning("Webhook auth FAILED from %s%s", ip, " - address now blocked" if blocked else "")
            return error(401, "unauthorized")  # same message whatever was wrong

        try:
            # Validate from JSON text: strict about types, yet accepts ISO date strings.
            payload = WebhookSignal.model_validate_json(json.dumps(data))
        except ValidationError as e:
            return error(422, _friendly_errors(e))

        now = clock()
        if payload.timestamp is not None:
            ts = payload.timestamp if payload.timestamp.tzinfo else payload.timestamp.replace(tzinfo=IST)
            age = (now - ts).total_seconds()
            if age > cfg.max_age_seconds or age < -cfg.max_age_seconds:
                return error(422, f"timestamp is {age:.0f}s away from now; limit is {cfg.max_age_seconds:g}s")
        try:
            signal = to_signal(payload, master, cfg, now, quotes)
        except ValueError as e:
            return error(422, str(e))

        try:
            decision = await run_in_threadpool(submit, signal)
        except Exception:
            log.exception("submit failed for webhook signal %s", signal.idempotency_key)
            return error(500, "internal error; the signal was NOT executed")
        if decision.approved:
            return JSONResponse({"status": "approved", "idempotency_id": payload.idempotency_id,
                                 "detail": "approved by the risk engine; order placement started"})
        return JSONResponse({"status": "rejected", "idempotency_id": payload.idempotency_id,
                             "reasons": [{"code": r.code, "detail": r.detail} for r in decision.reasons]})

    return app


def serve(app: FastAPI, cfg: WebhookConfig) -> None:
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning", server_header=False)


if __name__ == "__main__":
    from .runtime import main

    main()
