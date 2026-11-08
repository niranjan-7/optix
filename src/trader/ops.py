"""Day-to-day operations: heartbeat, daily restart, broker-login upkeep.

* HEARTBEAT closes the gap alerts cannot: a program that has crashed cannot tell you so. Every
  minute we ping an outside "dead man's switch" service that YOU choose (for example a free
  healthchecks.io check). If the pings stop, that service messages you. The URL contains a secret,
  so it is never logged.
* DAILY RESTART makes a long-running system start each day clean: a fresh instrument master (new
  contracts and expiries), a fresh broker login and a fresh option window. The process exits and
  Docker (restart: unless-stopped) starts it again. It is postponed while any position is open:
  we never restart on top of a live position.
* TOKEN KEEPER refreshes the broker login before it goes stale, so a reconnect never fails on an
  expired session.

All of it is best-effort: nothing here may ever disturb trading, so every task catches its own errors.
"""
from __future__ import annotations

import logging
import time as time_mod
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .greeks import IST

log = logging.getLogger(__name__)


class OpsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    daily_restart: time = time(8, 30)
    restart_postponed_alert_seconds: float = Field(1800, gt=0)
    heartbeat_seconds: float = Field(60, gt=0)
    token_check_seconds: float = Field(60, gt=0)

    @field_validator("daily_restart", mode="before")
    @classmethod
    def _quoted(cls, v: Any) -> Any:
        if not isinstance(v, (str, time)):
            raise ValueError('write the time as a quoted string, e.g. "08:30"')
        return v


def load_ops_config(path: str | Path = "config/ops.yaml") -> OpsConfig:
    with open(path, encoding="utf-8") as f:
        return OpsConfig(**(yaml.safe_load(f) or {}))


# ================================================================= heartbeat

class Heartbeat:
    """Pings an external watchdog URL at an interval. Never raises, never logs the URL."""

    def __init__(self, url: str, interval: float, opener: Callable[..., Any] = urllib.request.urlopen,
                 mono: Callable[[], float] = time_mod.monotonic):
        self._url, self._interval, self._open, self._mono = url, interval, opener, mono
        self._last: float | None = None
        self._failures = 0
        self.pings = 0

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def tick(self) -> bool:
        """Ping if due. Returns True if a ping was attempted."""
        if not self._url:
            return False
        now = self._mono()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        try:
            with self._open(self._url, timeout=5) as r:
                r.read(1)
            self.pings += 1
            if self._failures:
                log.info("Heartbeat ping working again after %d failure(s)", self._failures)
            self._failures = 0
        except Exception as e:
            self._failures += 1
            if self._failures in (1, 10) or self._failures % 60 == 0:  # don't flood the log
                # Only the exception TYPE: the message can contain the URL, which is a secret.
                log.warning("Heartbeat ping failed (%s); %d in a row", type(e).__name__, self._failures)
        return True


# ============================================================== daily restart

def next_restart_after(start: datetime, at: time) -> datetime:
    """The first moment, after `start`, that the clock reads `at` (in IST)."""
    start = start.astimezone(IST)
    candidate = datetime.combine(start.date(), at, IST)
    return candidate if candidate > start else candidate + timedelta(days=1)


class DailyRestart:
    def __init__(self, at: time, started_at: datetime, clock: Callable[[], datetime],
                 busy: Callable[[], bool], request: Callable[[], None],
                 notify: Callable[[str, str], None], postponed_alert_seconds: float = 1800,
                 mono: Callable[[], float] = time_mod.monotonic):
        self._due = next_restart_after(started_at, at)
        self._clock, self._busy, self._request, self._notify = clock, busy, request, notify
        self._alert_every, self._mono = postponed_alert_seconds, mono
        self._last_alert: float | None = None
        self.requested = False

    @property
    def due_at(self) -> datetime:
        return self._due

    def tick(self) -> None:
        if self.requested or self._clock().astimezone(IST) < self._due:
            return
        if self._busy():
            now = self._mono()
            if self._last_alert is None or now - self._last_alert >= self._alert_every:
                self._last_alert = now
                self._notify("WARNING", "Daily restart POSTPONED: a position or order is still open. "
                                        "Close it (or /kill) and the system will restart by itself.")
            return
        self.requested = True
        self._notify("INFO", "🔄 Daily restart: starting fresh with today's contracts and a new broker login.")
        self._request()


# ============================================================== login upkeep

class TokenKeeper:
    def __init__(self, auth: Any, interval: float, notify: Callable[[str, str], None],
                 mono: Callable[[], float] = time_mod.monotonic):
        self._auth, self._interval, self._notify, self._mono = auth, interval, notify, mono
        self._last: float | None = None
        self._last_alert: float | None = None

    def tick(self) -> None:
        now = self._mono()
        if self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        try:
            self._auth.ensure_session()  # logs in again only when the session is older than its limit
        except Exception as e:
            if self._last_alert is None or now - self._last_alert > 600:
                self._last_alert = now
                self._notify("WARNING", f"Broker login refresh failed: {e}. Market data may stop; check the system.")


# ================================================================== the loop

class OpsLoop:
    """Called once a second by the scheduler; each part keeps its own timing."""

    def __init__(self, tasks: list[Any]):
        self._tasks = [t for t in tasks if t is not None]

    def tick(self) -> None:
        for task in self._tasks:
            try:
                task.tick()
            except Exception:
                log.exception("ops task %s failed", type(task).__name__)
