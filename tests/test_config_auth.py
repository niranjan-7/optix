import logging

import pyotp
import pytest

from trader import logging_utils
from trader.auth import AngelAuth, AuthError
from trader.config import ConfigError, Settings, load_settings

TOTP = pyotp.random_base32()


def make_settings(**kw) -> Settings:
    base = dict(
        angel_api_key="KEY12345",
        angel_client_id="A12345",
        angel_pin="9876",
        angel_totp_secret=TOTP,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


class FakeClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def generateSession(self, cid, pin, code):
        self.calls.append((cid, pin, code))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp

    def getfeedToken(self):
        return "FEED"


OK = {"status": True, "data": {"jwtToken": "J", "refreshToken": "R"}}


def test_live_trading_defaults_false(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    assert Settings(_env_file=None).live_trading is False


def test_repr_does_not_leak_secrets():
    s = make_settings()
    text = repr(s) + str(s)
    assert "KEY12345" not in text and "9876" not in text and TOTP not in text


def test_missing_credentials_clear_error():
    with pytest.raises(ConfigError) as e:
        Settings(_env_file=None).require_angel_credentials()
    assert "ANGEL_API_KEY" in str(e.value) and ".env.example" in str(e.value)


def test_login_success_uses_valid_totp():
    fc = FakeClient(OK)
    auth = AngelAuth(make_settings(), client_factory=lambda k: fc)
    sess = auth.login()
    assert sess.jwt_token == "J" and sess.feed_token == "FEED"
    code = fc.calls[0][2]
    assert pyotp.TOTP(TOTP).verify(code)


def test_login_rejected_gives_clear_error():
    fc = FakeClient({"status": False, "message": "Invalid TOTP"})
    with pytest.raises(AuthError) as e:
        AngelAuth(make_settings(), client_factory=lambda k: fc).login()
    assert "Invalid TOTP" in str(e.value) and "clock" in str(e.value)


def test_login_network_error_wrapped_without_secrets():
    fc = FakeClient(ConnectionError("boom 9876"))
    with pytest.raises(AuthError) as e:
        AngelAuth(make_settings(), client_factory=lambda k: fc).login()
    assert "9876" not in str(e.value)


def test_bad_totp_secret():
    with pytest.raises(AuthError):
        AngelAuth(make_settings(angel_totp_secret="not base32!!"), client_factory=lambda k: FakeClient(OK)).login()


def test_ensure_session_refreshes_after_max_age():
    t = [1000.0]
    fc = FakeClient(OK)
    auth = AngelAuth(make_settings(), client_factory=lambda k: fc, max_age_hours=1, clock=lambda: t[0])
    auth.ensure_session()
    auth.ensure_session()
    assert len(fc.calls) == 1  # still fresh
    t[0] += 3601
    auth.ensure_session()
    assert len(fc.calls) == 2  # re-login


def test_log_redaction(caplog):
    logging_utils.register_secret("SUPERSECRET")
    f = logging_utils.RedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, "", 0, "token=%s", ("SUPERSECRET",), None)
    f.filter(rec)
    assert "SUPERSECRET" not in rec.getMessage() and "***" in rec.getMessage()


def test_load_settings_registers_secrets(monkeypatch):
    monkeypatch.setenv("ANGEL_PIN", "55556666")
    load_settings()
    assert "***" in logging_utils.redact("pin 55556666")


def test_session_tokens_are_scrubbed_from_logs():
    resp = {"status": True, "data": {"jwtToken": "JWT-SECRET-VALUE", "refreshToken": "REFRESH-SECRET-VALUE"}}
    fc = FakeClient(resp)
    fc.getfeedToken = lambda: "FEED-SECRET-VALUE"
    AngelAuth(make_settings(), client_factory=lambda k: fc).login()
    for tok in ("JWT-SECRET-VALUE", "REFRESH-SECRET-VALUE", "FEED-SECRET-VALUE"):
        assert tok not in logging_utils.redact(f"header {tok}")


# ------------------------------------------------------------ `python -m trader.auth`

def _cli(monkeypatch, creds, factory):
    from trader import auth as auth_mod
    for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in creds.items():
        monkeypatch.setenv(k, v)
    out = []
    code = auth_mod._cli([], auth_factory=factory, out=out.append)
    return code, "\n".join(out)


GOOD_CREDS = {"ANGEL_API_KEY": "KEY12345", "ANGEL_CLIENT_ID": "A12345", "ANGEL_PIN": "9876", "ANGEL_TOTP_SECRET": TOTP}


def test_login_check_reports_success_without_printing_secrets(monkeypatch):
    class Ok:
        def __init__(self, s): pass
        def login(self): return None
    code, out = _cli(monkeypatch, GOOD_CREDS, Ok)
    assert code == 0 and "Login OK for client A12345" in out and "No orders were placed" in out
    assert not any(secret in out for secret in ("KEY12345", "9876", TOTP))


def test_login_check_explains_a_refusal_and_missing_settings(monkeypatch):
    class Refused:
        def __init__(self, s): pass
        def login(self): raise AuthError("Angel One rejected the login: Invalid TOTP. Check your clock.")
    code, out = _cli(monkeypatch, GOOD_CREDS, Refused)
    assert code == 1 and "Login FAILED" in out and "Invalid TOTP" in out
    code, out = _cli(monkeypatch, {}, Refused)
    assert code == 2 and "ANGEL_API_KEY" in out and ".env" in out
