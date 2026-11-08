"""Angel One SmartAPI login with TOTP and daily token refresh.

Angel One sessions expire daily, so `ensure_session()` re-logs in when the
session is older than `max_age_hours`. The scheduler (later module) calls it
before market open; the feed/broker call it on auth errors.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import pyotp

from .config import ConfigError, Settings
from .logging_utils import register_secret

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Login failed; message is safe to show an operator (no secrets)."""


@dataclass
class Session:
    jwt_token: str
    refresh_token: str
    feed_token: str
    created_at: float


def _default_client_factory(api_key: str) -> Any:
    # Imported lazily so tests and paper/simulated runs don't need the SDK.
    from SmartApi import SmartConnect

    return SmartConnect(api_key=api_key)


class AngelAuth:
    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[str], Any] = _default_client_factory,
        max_age_hours: float = 12.0,
        clock: Callable[[], float] = time.time,
    ):
        self._s = settings
        self._factory = client_factory
        self._max_age = max_age_hours * 3600
        self._clock = clock
        self.client: Any = None
        self.session: Session | None = None

    def login(self) -> Session:
        try:
            self._s.require_angel_credentials()
        except ConfigError as e:
            raise AuthError(str(e)) from e

        # TOTP codes last 30s; generate right before use.
        try:
            code = pyotp.TOTP(self._s.angel_totp_secret.get_secret_value()).now()
        except Exception as e:  # bad base32 secret
            raise AuthError(
                "ANGEL_TOTP_SECRET is not a valid TOTP secret (expected the base32 "
                "text shown when enabling TOTP, not a 6-digit code)."
            ) from e

        client = self._factory(self._s.angel_api_key.get_secret_value())
        try:
            resp = client.generateSession(
                self._s.angel_client_id,
                self._s.angel_pin.get_secret_value(),
                code,
            )
        except Exception as e:
            # Don't include str(e) blindly; the redaction filter protects logs,
            # but keep the operator message generic and actionable.
            raise AuthError(
                f"Could not reach Angel One for login ({type(e).__name__}). "
                "Check internet connection and try again."
            ) from e

        if not resp or not resp.get("status"):
            msg = (resp or {}).get("message", "no response")
            raise AuthError(
                f"Angel One rejected the login: {msg}. Check client ID, PIN, "
                "API key, TOTP secret, and that your computer's clock is correct."
            )

        data = resp["data"]
        # Session tokens are credentials too: scrub them from all log output.
        for tok in (data["jwtToken"], data["refreshToken"], client.getfeedToken()):
            register_secret(tok)
        self.client = client
        self.session = Session(
            jwt_token=data["jwtToken"],
            refresh_token=data["refreshToken"],
            feed_token=client.getfeedToken(),
            created_at=self._clock(),
        )
        log.info("Angel One login OK for client %s", self._s.angel_client_id)
        return self.session

    def is_stale(self) -> bool:
        return self.session is None or (self._clock() - self.session.created_at) > self._max_age

    def ensure_session(self) -> Session:
        """Log in if there is no session or it is older than max_age_hours."""
        if self.is_stale():
            return self.login()
        assert self.session is not None
        return self.session


def _cli(argv: list[str] | None = None, auth_factory: Callable[[Settings], Any] = AngelAuth,
         out: Callable[[str], None] = print) -> int:
    """`python -m trader.auth`: check that your Angel One login settings work.

    It only logs in. It places no orders, and never prints a password, key or token.
    Exit code: 0 = login worked, 1 = Angel One refused or could not be reached, 2 = settings missing.
    """
    from .config import load_settings

    s = load_settings()
    try:
        s.require_angel_credentials()
    except ConfigError as e:
        out(str(e))
        return 2
    try:
        auth_factory(s).login()
    except AuthError as e:
        out(f"Login FAILED: {e}")
        return 1
    out(f"Login OK for client {s.angel_client_id}. Your Angel One settings work. (No orders were placed.)")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli())
