"""Logging setup with secret redaction.

Why: rule 3 says secrets must never reach logs. Rather than trusting every
call site, we scrub every record at the handler level: any registered secret
value is replaced by '***' wherever it appears in the final message.
"""
from __future__ import annotations

import logging

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember a secret so it is scrubbed from all future log output."""
    # Ignore very short values to avoid mangling normal text (e.g. "1").
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Format first so secrets inside %-args are caught too.
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
