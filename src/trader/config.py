"""Settings loaded from .env (see .env.example).

Why SecretStr: str()/repr() of a SecretStr prints '**********', so accidental
logging of the settings object cannot leak credentials.
"""
from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .logging_utils import register_secret


class ConfigError(RuntimeError):
    """Raised with an operator-friendly message when config is unusable."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Safe default: paper. Rule 1.
    live_trading: bool = False
    # Where prices come from. "live" = real Angel One prices (needs credentials); "simulated" = made-up
    # prices for practising without an account. Either way orders go to the PAPER broker unless live trading
    # is separately and deliberately enabled.
    trader_feed: Literal["live", "simulated"] = "live"

    angel_api_key: SecretStr = SecretStr("")
    angel_client_id: str = ""
    angel_pin: SecretStr = SecretStr("")
    angel_totp_secret: SecretStr = SecretStr("")

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # Optional "dead man's switch" URL (e.g. a healthchecks.io check). We ping it every minute; if the pings
    # stop, that service messages you. It contains a secret, so it is treated like a password.
    heartbeat_url: SecretStr = SecretStr("")

    webhook_secret: SecretStr = SecretStr("")
    # Optional. When set, every webhook request must also carry a valid HMAC signature.
    webhook_hmac_secret: SecretStr = SecretStr("")

    def secret_values(self) -> list[str]:
        return [
            self.angel_api_key.get_secret_value(),
            self.angel_pin.get_secret_value(),
            self.angel_totp_secret.get_secret_value(),
            self.telegram_bot_token.get_secret_value(),
            self.webhook_secret.get_secret_value(),
            self.webhook_hmac_secret.get_secret_value(),
            self.heartbeat_url.get_secret_value(),
        ]

    def require_angel_credentials(self) -> None:
        missing = [
            name
            for name, val in {
                "ANGEL_API_KEY": self.angel_api_key.get_secret_value(),
                "ANGEL_CLIENT_ID": self.angel_client_id,
                "ANGEL_PIN": self.angel_pin.get_secret_value(),
                "ANGEL_TOTP_SECRET": self.angel_totp_secret.get_secret_value(),
            }.items()
            if not val
        ]
        if missing:
            raise ConfigError(
                "Missing Angel One settings in .env: " + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )


MIN_SECRET_LEN = 16


def require_webhook_secret(s: "Settings") -> None:
    """The webhook is reachable from the internet; refuse to run it with a weak secret."""
    for name, val in (("WEBHOOK_SECRET", s.webhook_secret.get_secret_value()),):
        if len(val) < MIN_SECRET_LEN:
            raise ConfigError(
                f"{name} in .env is missing or shorter than {MIN_SECRET_LEN} characters. "
                "Generate one with:  python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    h = s.webhook_hmac_secret.get_secret_value()
    if h and len(h) < MIN_SECRET_LEN:
        raise ConfigError(f"WEBHOOK_HMAC_SECRET is shorter than {MIN_SECRET_LEN} characters (or leave it empty to disable HMAC).")


def load_settings() -> Settings:
    """Load settings and register all secrets with the log redactor."""
    s = Settings()
    for v in s.secret_values():
        register_secret(v)
    return s
