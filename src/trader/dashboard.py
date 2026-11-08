"""Local dashboard: live option chain with IV/Greeks, positions, P&L, limits, feed health, KILL button.

Served by the same FastAPI app as the webhook. Because that app can be reached from
the internet through the tunnel, this module is deliberately locked down:

* Tunnel traffic can never see it (the webhook module answers 404 for anything but
  /webhook and /health when Cloudflare headers are present).
* Every dashboard request must carry a localhost `Host` header. This defeats "DNS
  rebinding", where a hostile website tricks your browser into talking to your own
  computer under another name.
* The KILL endpoint only accepts a same-origin POST that carries a custom header, so
  another website cannot trigger it from your browser.
* There is a KILL button but NO reset button, on purpose: turning trading back on is a
  typed command (`python -m trader.risk reset`), never a stray click.
* Strict Content-Security-Policy; the page uses no external scripts, fonts or images
  and builds its tables with textContent (never innerHTML).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .feed import HealthStatus, Instr, Tick
from .greeks import IST, analyse, time_to_expiry
from .instruments import InstrumentMaster
from .portfolio import Portfolio
from .risk import RiskEngine
from .webhook import PUBLIC_PATHS

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
STALE_QUOTE_SECONDS = 30.0
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _f(x: Any) -> float | None:
    """JSON cannot carry NaN/Infinity; turn anything non-finite into null."""
    try:
        return float(x) if x is not None and math.isfinite(float(x)) else None
    except (TypeError, ValueError):
        return None


def hostname(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class DashboardData:
    """Builds the JSON the page shows. Read-only; it never changes any state."""

    def __init__(self, *, underlying: str, expiry, master: InstrumentMaster,
                 latest: Callable[[Instr], Tick | None], index: Instr, future: Instr,
                 portfolio: Portfolio, risk: RiskEngine, feed_health: Callable[[], HealthStatus],
                 strikes_each_side: int, journal: Any = None, live: bool = False,
                 data_source: str = "simulated", active_executions: Callable[[], int] = lambda: 0,
                 clock: Callable[[], datetime] | None = None):
        self._u, self._expiry, self._master = underlying, expiry, master
        self._latest, self._index, self._future = latest, index, future
        self._pf, self._risk, self._health, self._n = portfolio, risk, feed_health, strikes_each_side
        self._journal, self._live, self._source = journal, live, data_source
        self._active = active_executions
        self._clock = clock or (lambda: datetime.now(IST))

    # ------------------------------------------------------------ chain
    def _leg(self, strike: float, opt_type: str, fut: float | None, T: float, now: datetime) -> dict[str, Any]:
        opt = self._master.find_option(self._u, self._expiry, strike, opt_type)
        q = self._latest(Instr("NFO", opt.token))
        row: dict[str, Any] = {"symbol": opt.symbol, "ltp": None, "bid": None, "ask": None, "age": None, "stale": True,
                               "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
        if q is None:
            return row
        age = max(0.0, now.timestamp() - q.received_at)
        row.update(ltp=_f(q.ltp), bid=_f(q.bid), ask=_f(q.ask), age=_f(round(age, 1)), stale=age > STALE_QUOTE_SECONDS)
        price = (q.bid + q.ask) / 2 if q.bid and q.ask else q.ltp
        if fut and price and price > 0:
            a = analyse(price, fut, strike, T, opt_type)  # futures as the underlying (Black-76)
            if a.iv is not None and a.greeks is not None:
                row.update(iv=_f(a.iv * 100), delta=_f(a.greeks.delta), gamma=_f(a.greeks.gamma),
                           theta=_f(a.greeks.theta), vega=_f(a.greeks.vega))
        return row

    def _chain(self, now: datetime, ref: float | None, fut: float | None) -> tuple[list[dict], float | None]:
        if ref is None:
            return [], None
        strikes = self._master.strikes_around_atm(self._u, self._expiry, ref, self._n)
        atm = self._master.atm_strike(self._u, self._expiry, ref)
        T = time_to_expiry(now, self._expiry)
        return ([{"strike": k, "atm": k == atm, "ce": self._leg(k, "CE", fut, T, now), "pe": self._leg(k, "PE", fut, T, now)}
                 for k in strikes], atm)

    # ------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        now = self._clock().astimezone(IST)
        h = self._health()
        st = self._risk.status()
        cfg = self._risk.cfg
        spot_t, fut_t = self._latest(self._index), self._latest(self._future)
        spot, fut = _f(spot_t.ltp if spot_t else None), _f(fut_t.ltp if fut_t else None)
        chain, atm = self._chain(now, spot if spot is not None else fut, fut)
        realised, unrealised = self._pf.realised_pnl_today(), self._pf.unrealised_pnl()
        t = now.time()
        window_open = (now.weekday() in cfg.window.weekdays
                       and cfg.window.trading_start <= t <= min(cfg.window.no_new_entries_after, cfg.window.trading_end))
        return {
            "time": now.isoformat(),
            "mode": "LIVE" if self._live else "PAPER",
            "data_source": self._source,
            "feed": {"healthy": h.healthy, "connected": h.connected, "reason": h.reason,
                     "seconds_since_tick": _f(h.seconds_since_tick)},
            "market": {"underlying": self._u, "spot": spot, "future": fut, "atm": atm, "expiry": self._expiry.isoformat(),
                       "days_to_expiry": (self._expiry - now.date()).days,
                       "lot_size": self._master.lot_size(self._u, self._expiry)},
            "chain": chain,
            "positions": [
                {**{k: v for k, v in p.items() if k not in ("legs", "unrealised", "stop_loss", "target")},
                 "stop_loss": _f(p["stop_loss"]), "target": _f(p["target"]), "unrealised": _f(p["unrealised"]),
                 "legs": [{**l, "entry": _f(l["entry"]), "mark": _f(l["mark"]), "pnl": _f(l["pnl"])} for l in p["legs"]]}
                for p in self._pf.positions_detail()],
            "pnl": {"realised": _f(realised), "unrealised": _f(unrealised), "total": _f(realised + unrealised)},
            "risk": {
                "kill_active": st["kill_active"], "kill_reason": st["kill_reason"], "kill_source": st["kill_source"],
                "day_halted": st["day_halted"], "halt_reason": st["halt_reason"],
                "loss_limit": cfg.limits.max_loss_per_day, "loss_used": max(0.0, -(realised + unrealised)),
                "orders_used": st["orders_used"], "orders_limit": st["orders_limit"],
                "positions_open": st["open_positions"], "positions_limit": st["positions_limit"],
                "max_loss_per_trade": cfg.limits.max_loss_per_trade, "max_lots_per_order": cfg.limits.max_lots_per_order,
                "entry_window": f"{cfg.window.trading_start:%H:%M}-{min(cfg.window.no_new_entries_after, cfg.window.trading_end):%H:%M}",
                "window_open": window_open, "force_square_off": f"{cfg.window.force_square_off:%H:%M}",
                "allowed_structures": cfg.structures.allowed,
            },
            "active_executions": self._active(),
            "recent_decisions": self._journal.recent_signals(10) if self._journal else [],
        }


def install_dashboard(app: FastAPI, data: DashboardData, kill: Callable[[str], None]) -> None:
    """Add the dashboard routes to `app`. `kill(reason)` must engage the kill switch."""

    @app.middleware("http")
    async def dashboard_guard(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)
        if hostname(request.headers.get("host", "")) not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)  # DNS-rebinding defence
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def asset(name: str, media: str) -> Response:
        return FileResponse(STATIC / name, media_type=media)

    @app.get("/")
    async def page() -> Response:
        return asset("dashboard.html", "text/html; charset=utf-8")

    @app.get("/static/dashboard.js")
    async def js() -> Response:
        return asset("dashboard.js", "text/javascript; charset=utf-8")

    @app.get("/static/dashboard.css")
    async def css() -> Response:
        return asset("dashboard.css", "text/css; charset=utf-8")

    @app.get("/api/state")
    async def state() -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool(data.state))
        except Exception:
            log.exception("dashboard state failed")
            return JSONResponse({"detail": "state unavailable"}, status_code=500)

    @app.post("/api/kill")
    async def kill_switch(request: Request) -> JSONResponse:
        # Same-origin only: browsers will not attach a custom header on a cross-site request
        # without a CORS preflight, which this server never grants. Origin/Sec-Fetch-Site
        # are checked as well, belt and braces.
        origin = request.headers.get("origin")
        if (request.headers.get("x-requested-with") != "dashboard"
                or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none")
                or (origin is not None and hostname(urlparse(origin).netloc) not in ALLOWED_HOSTS)):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        reason = "dashboard KILL button"
        try:
            await run_in_threadpool(kill, reason)
        except Exception:
            log.exception("KILL from the dashboard FAILED")
            # The switch flips before any broker action is tried, so it is very likely ON; say so honestly.
            return JSONResponse({"status": "error", "detail": "kill raised an error; check the kill switch state and your positions"},
                                status_code=500)
        return JSONResponse({"status": "kill_switch_on"})
