"""Telegram alerts and phone commands.

OUTBOUND (to you): every signal (approved or refused, with reasons), order, fill, closed
position, risk-limit hit, kill switch, feed trouble, and a daily summary.

INBOUND (from you): /status /positions /summary /kill and a two-step /reset, accepted ONLY
from the one private chat whose ID is in .env.

Design rules (the "why"):
* Telegram problems must never disturb trading. Sending happens on a background thread
  from a bounded queue; a dead network just means alerts are dropped and logged.
* The bot token is a credential and sits in the URL of every request. It is never logged,
  never put in an exception message, and the HTTP error objects (which carry the URL) are
  deliberately discarded.
* A command is only obeyed if it is from the right private chat, from a human, and FRESH.
  Telegram keeps unread messages for a day, so after a restart an old "/kill" or
  "/reset" could otherwise replay: the backlog is thrown away at start-up and anything
  older than a minute is ignored.
* A flood of routine alerts must not bury the important ones: routine (INFO) messages are
  rate-limited, with a "N held back" note on the next message; warnings, critical alerts and
  replies to your commands always go (identical warnings are folded together instead).
* Plain text only (no Markdown/HTML), so nothing in a message can be misread as markup.
"""
from __future__ import annotations

import argparse
import collections
import hmac
import json
import logging
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .greeks import IST
from .ratelimit import TokenBucket
from .signals import Signal

log = logging.getLogger(__name__)

INFO, WARNING, CRITICAL, REPLY = "INFO", "WARNING", "CRITICAL", "REPLY"
# Only routine INFO alerts are ever held back by flood control; warnings, critical alerts and replies always go.
ALWAYS_SEND = {WARNING, CRITICAL, REPLY}
MAX_TEXT = 3500  # Telegram's limit is 4096 characters
ICON = {INFO: "ℹ️", WARNING: "⚠️", CRITICAL: "🚨", REPLY: "💬"}


# ================================================================== config

class AlertsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    enabled: bool = True
    send_signals: bool = True           # approved signals
    send_rejections: bool = True        # refused signals (your "rule violations")
    send_orders: bool = True            # order placed / cancelled
    send_fills: bool = True             # fills and closed positions
    feed_unhealthy_after_seconds: float = Field(15, gt=0)
    feed_reminder_seconds: float = Field(300, gt=0)
    market_open: dtime = dtime(9, 15)
    market_close: dtime = dtime(15, 30)
    daily_summary_time: dtime = dtime(15, 45)
    max_messages_per_minute: float = Field(20, gt=0)
    burst: int = Field(8, ge=1)
    max_queue: int = Field(200, ge=10)
    repeat_window_seconds: float = Field(60, ge=0)   # identical refusals inside this window are folded together
    accept_commands: bool = True
    command_poll_seconds: int = Field(25, ge=1, le=50)
    command_max_age_seconds: float = Field(60, gt=0)
    reset_code_ttl_seconds: float = Field(120, gt=0)
    state_file: str = "data/alerts_state.json"

    @field_validator("market_open", "market_close", "daily_summary_time", mode="before")
    @classmethod
    def _quoted(cls, v: Any) -> Any:
        if not isinstance(v, (str, dtime)):
            raise ValueError('write times as quoted strings, e.g. "15:45"')
        return v


def load_alerts_config(path: str | Path = "config/alerts.yaml") -> AlertsConfig:
    with open(path, encoding="utf-8") as f:
        return AlertsConfig(**(yaml.safe_load(f) or {}))


# ============================================================ Telegram client

class TelegramError(RuntimeError):
    """A Telegram call failed. The message never contains the bot token."""

    def __init__(self, message: str, retry_after: float | None = None, code: int | None = None):
        super().__init__(message)
        self.retry_after, self.code = retry_after, code


def urllib_transport(token: str) -> Callable[[str, dict, float], dict]:
    """The only place the token is used: it goes into the URL and nowhere else."""

    def call(method: str, payload: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()  # Telegram describes errors in a JSON body, even on 4xx
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # `from None`: the original exception can carry the request URL (and so the token).
            raise TelegramError(f"could not reach Telegram ({type(e).__name__})") from None
        try:
            return json.loads(body)
        except ValueError:
            raise TelegramError("Telegram sent a reply that is not JSON") from None

    return call


class TelegramApi:
    def __init__(self, transport: Callable[[str, dict, float], dict], timeout: float = 15.0):
        self._transport, self._timeout = transport, timeout

    def _call(self, method: str, payload: dict, timeout: float | None = None) -> Any:
        data = self._transport(method, payload, timeout or self._timeout)
        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise TelegramError(f"Telegram said: {data.get('description', 'unknown error')}",
                                retry_after=params.get("retry_after"), code=data.get("error_code"))
        return data.get("result")

    def get_me(self) -> dict:
        return self._call("getMe", {})

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True})

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=timeout + 10) or []


# ================================================================ formatting

def _rs(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"Rs {x:+,.0f}" if signed else f"Rs {x:,.0f}"


def _legs(sig: Signal) -> str:
    return ", ".join(f"{l.side} {l.lots} lot{'s' if l.lots != 1 else ''} ({l.qty}) {l.symbol}" for l in sig.legs)


def fmt_decision(signal: Signal, decision: Any) -> tuple[str, str, str]:
    """-> (level, text, dedupe_key)."""
    when = signal.created_at.astimezone(IST).strftime("%H:%M:%S")
    head = f"{signal.strategy} · {signal.kind} · {_legs(signal)}"
    if decision.approved:
        a = decision.order.assessment if decision.order else None
        lines = [f"✅ SIGNAL APPROVED {when}", head]
        if signal.kind == "ENTRY":
            lvl = []
            if signal.stop_loss is not None:
                lvl.append(f"stop {signal.stop_loss:g}")
            if signal.target is not None:
                lvl.append(f"target {signal.target:g}")
            if a and a.loss_at_stop is not None:
                lvl.append(f"risk at stop {_rs(a.loss_at_stop)}")
            if lvl:
                lines.append(" · ".join(lvl))
        lines.append(f"Why: {signal.reason}")
        return INFO, "\n".join(lines), ""
    lines = [f"⛔ SIGNAL REFUSED {when}", head]
    lines += [f"• {r.code}: {r.detail}" for r in decision.reasons]
    lines.append(f"Source: {signal.source}  ·  id {signal.idempotency_key}")
    return INFO, "\n".join(lines), f"{signal.strategy}|{signal.kind}|{','.join(decision.codes)}"


_CRITICAL_WORDS = ("CHECK POSITIONS", "MISMATCH", "MANUALLY", "NOT BOOKED", "CLOSE IT")


def fmt_event(ev: dict[str, Any], cfg: AlertsConfig) -> tuple[str, str] | None:
    """Execution event -> (level, text), or None for events not worth a message."""
    t = ev.get("type")
    if t == "order_placed" and cfg.send_orders:
        return INFO, f"📤 ORDER {ev['side']} {ev['lots']}× {ev['symbol']} at limit {ev['limit_price']:g}"
    if t == "fill" and cfg.send_fills:
        return INFO, f"✔️ FILL {ev['side']} {ev['lots']}× {ev['symbol']} @ {ev['price']:g}  (fee {_rs(ev['fee'])})"
    if t == "position_closed" and cfg.send_fills:
        return INFO, (f"🏁 POSITION {ev['position_id']} CLOSED  P&L {_rs(ev['realised_pnl'], True)} "
                      f"(after fees {_rs(ev['fees'])})  [{ev.get('strategy', '')}]")
    if t == "order_cancel_sent" and cfg.send_orders:
        return INFO, f"🚫 Order {ev['order_id']} cancel sent ({ev.get('reason', '')})"
    if t == "leg_skipped":
        return WARNING, f"⚠️ Leg not placed: {ev['symbol']} {ev['side']}: {ev['reason']}"
    if t == "cancel_all":
        return INFO, f"🚫 Cancelled all pending orders ({ev.get('broker_orders_cancelled', 0)} at the broker)"
    if t == "warning":
        msg = str(ev.get("message", ""))
        return (CRITICAL if any(w in msg.upper() for w in _CRITICAL_WORDS) else WARNING), f"⚠️ {msg}"
    return None


# ============================================================ sending (async)

class AlertSender:
    """Background sender: bounded queue, flood control, retries. Never blocks the caller."""

    def __init__(self, api: TelegramApi, chat_id: int, cfg: AlertsConfig,
                 mono: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._api, self._chat, self._cfg, self._sleep = api, chat_id, cfg, sleep
        self._bucket = TokenBucket(cfg.max_messages_per_minute, cfg.burst, mono)
        self._q: collections.deque[tuple[str, str]] = collections.deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.suppressed = 0
        self.sent = 0
        self.failed = 0

    def submit(self, level: str, text: str) -> None:
        if level not in ALWAYS_SEND and self._bucket.take() > 0:
            self.suppressed += 1  # flood control: routine messages only
            return
        if self.suppressed and level != REPLY:
            # The note goes on the next message AFTER the gap, so it sits where the gap happened.
            text = f"(+{self.suppressed} routine alerts held back to avoid flooding)\n{text}"
            self.suppressed = 0
        with self._lock:
            self._q.append((level, text))
            while len(self._q) > self._cfg.max_queue:  # Telegram unreachable for a long time: drop the oldest routine one
                drop = next((i for i, (lv, _) in enumerate(self._q) if lv not in ALWAYS_SEND), 0)
                del self._q[drop]
                self.failed += 1
        self._wake.set()

    def drain(self) -> None:
        """Send everything queued, now, on this thread."""
        while True:
            with self._lock:
                if not self._q:
                    return
                level, text = self._q.popleft()
            self._send(level, text)

    def _send(self, level: str, text: str) -> None:
        for attempt in range(4):
            try:
                self._api.send_message(self._chat, text)
                self.sent += 1
                return
            except TelegramError as e:
                if e.code in (400, 401, 403, 404):  # wrong token/chat, or blocked: retrying will not help
                    self.failed += 1
                    log.error("Telegram refused an alert permanently (%s). Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.", e)
                    return
                self._sleep(min(e.retry_after, 60) if e.retry_after else 2 ** attempt)
            except Exception:
                log.exception("Unexpected error sending a Telegram alert")
                self.failed += 1
                return
        self.failed += 1
        log.error("Gave up sending a Telegram alert after 4 attempts (the %s alert was lost)", level)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self.drain()
            except Exception:
                log.exception("alert sender loop error")

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self.drain()  # last words, e.g. a final summary
        except Exception:
            pass


# ======================================================== the alerts facade

class Alerts:
    """What the rest of the system talks to. Does nothing (safely) when Telegram is not configured."""

    def __init__(self, sender: AlertSender | None, cfg: AlertsConfig, live: bool = False,
                 clock: Callable[[], datetime] | None = None, mono: Callable[[], float] = time.monotonic):
        self._sender, self.cfg, self._mono = sender, cfg, mono
        self._clock = clock or (lambda: datetime.now(IST))
        self.tag = "[LIVE]" if live else "[PAPER]"
        self._recent: dict[str, tuple[float, int]] = {}  # dedupe key -> (last sent, folded count)
        # late-bound collaborators (see bind)
        self._health: Callable[[], Any] | None = None
        self._open_positions: Callable[[], int] = lambda: 0
        self._weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
        self._journal: Any = None
        self._portfolio: Any = None
        self._risk: Any = None
        self._unhealthy_since: float | None = None
        self._feed_alerted_at: float | None = None
        self._last_summary: str | None = None
        self._state_path = Path(cfg.state_file)
        try:
            self._last_summary = json.loads(self._state_path.read_text()).get("last_summary")
        except Exception:
            pass

    @property
    def enabled(self) -> bool:
        return self._sender is not None and self.cfg.enabled

    def bind(self, *, health: Callable[[], Any], open_positions: Callable[[], int], weekdays: tuple[int, ...],
             journal: Any = None, portfolio: Any = None, risk: Any = None) -> None:
        self._health, self._open_positions, self._weekdays = health, open_positions, weekdays
        self._journal, self._portfolio, self._risk = journal, portfolio, risk

    def send(self, level: str, text: str, dedupe_key: str = "") -> None:
        if not self.enabled:
            return
        if dedupe_key and self.cfg.repeat_window_seconds > 0:
            now = self._mono()
            last, folded = self._recent.get(dedupe_key, (-1e9, 0))
            if now - last < self.cfg.repeat_window_seconds:
                self._recent[dedupe_key] = (last, folded + 1)
                return
            if folded:
                text += f"\n(the same refusal happened {folded} more time{'s' if folded != 1 else ''} in the last minute)"
            self._recent[dedupe_key] = (now, 0)
        assert self._sender is not None
        self._sender.submit(level, f"{self.tag} {text}")

    # ---------------------------------------------------------- listeners
    def on_decision(self, signal: Signal, decision: Any) -> None:
        """RiskEngine listener."""
        try:
            level, text, key = fmt_decision(signal, decision)
            if (decision.approved and not self.cfg.send_signals) or (not decision.approved and not self.cfg.send_rejections):
                return
            self.send(level, text, key)
        except Exception:
            log.exception("alert for a decision failed")

    def on_event(self, ev: dict[str, Any]) -> None:
        """ExecutionManager listener."""
        try:
            out = fmt_event(ev, self.cfg)
            if out:
                level, text = out
                # An identical warning repeating (e.g. "no quote for X") is folded, not repeated.
                self.send(level, text, dedupe_key=f"event|{text}" if level != INFO else "")
        except Exception:
            log.exception("alert for an execution event failed")

    def risk_notice(self, level: str, message: str) -> None:
        """RiskEngine `notifier`: kill switch, daily loss limit, failed emergency actions."""
        try:
            lv = CRITICAL if level.upper() == "CRITICAL" else WARNING
            self.send(lv, f"{ICON[lv]} {message}")
        except Exception:
            log.exception("alert for a risk notice failed")

    # ---------------------------------------------------- periodic checks
    def tick(self) -> None:
        """Call about once a second."""
        try:
            self.check_feed()
            self.maybe_daily_summary()
        except Exception:
            log.exception("alerts tick failed")

    def _market_open(self, now: datetime) -> bool:
        return now.weekday() in self._weekdays and self.cfg.market_open <= now.time() <= self.cfg.market_close

    def check_feed(self) -> None:
        if self._health is None or not self.enabled:
            return
        now, mono, h = self._clock(), self._mono(), self._health()
        if h.healthy or not self._market_open(now):
            if self._feed_alerted_at is not None and h.healthy:
                down = mono - (self._unhealthy_since or mono)
                self.send(INFO, f"🟢 Market data feed recovered (was down about {down:.0f}s).")
            self._unhealthy_since = self._feed_alerted_at = None
            return
        if self._unhealthy_since is None:
            self._unhealthy_since = mono
        down = mono - self._unhealthy_since
        due = (self._feed_alerted_at is None or mono - self._feed_alerted_at >= self.cfg.feed_reminder_seconds)
        if down >= self.cfg.feed_unhealthy_after_seconds and due:
            self._feed_alerted_at = mono
            n = self._open_positions()
            if n:
                self.send(CRITICAL, f"🚨 MARKET DATA DOWN for {down:.0f}s with {n} open position(s): stop-losses and "
                                    f"targets are NOT being watched. Reason: {h.reason}. Check the system now; "
                                    "send /kill to close everything.")
            else:
                self.send(WARNING, f"⚠️ Market data feed unhealthy for {down:.0f}s: {h.reason}. New entries are blocked.")

    def build_summary(self, day: date) -> str:
        lines = [f"📊 DAILY SUMMARY {day:%a %d-%b-%Y}"]
        if self._journal is not None:
            s = self._journal.summary(day, day)
            lines.append(f"Signals: {s.signals_total} seen, {s.signals_approved} approved, {s.signals_total - s.signals_approved} refused")
            if s.rejections_by_code:
                lines.append("Refused for: " + ", ".join(f"{c} ×{n}" for c, n in list(s.rejections_by_code.items())[:4]))
            if s.trades:
                lines.append(f"Closed trades: {s.trades} (won {s.wins}, lost {s.losses}, flat {s.breakeven})")
                lines.append(f"Net P&L (after fees): {_rs(s.net_pnl, True)}   fees {_rs(s.fees)}")
                lines.append(f"Best {_rs(s.best_trade, True)} · worst {_rs(s.worst_trade, True)}")
            else:
                lines.append("No trades closed today.")
            pending = len(self._journal.trades(day, day, unreviewed_only=True))
            if pending:
                lines.append(f"{pending} trade(s) need your evening review: python -m trader.journal review")
        if self._portfolio is not None:
            pos = self._portfolio.open_positions()
            lines.append(f"Open positions: {len(pos)}" + ("  ⚠️ still open after the close!" if pos else ""))
        if self._risk is not None:
            st = self._risk.status()
            lines.append(f"Orders used {st['orders_used']}/{st['orders_limit']} · kill switch {'ON' if st['kill_active'] else 'off'}"
                         + (" · day was halted by the loss limit" if st["day_halted"] else ""))
        return "\n".join(lines)

    def maybe_daily_summary(self) -> bool:
        now = self._clock()
        day = now.date().isoformat()
        if (not self.enabled or now.weekday() not in self._weekdays or now.time() < self.cfg.daily_summary_time
                or self._last_summary == day):
            return False
        self.send(INFO, self.build_summary(now.date()))
        self._last_summary = day
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"last_summary": day}))
        except OSError:
            log.exception("could not save alerts state (a summary may be re-sent after a restart)")
        return True

    # ------------------------------------------------------------ control
    def start(self) -> None:
        if self._sender:
            self._sender.start()

    def stop(self) -> None:
        if self._sender:
            self._sender.stop()


# =============================================================== commands

HELP = ("Commands (only you can use them):\n"
        "/status  – kill switch, feed, P&L, limits used\n"
        "/positions – open positions\n"
        "/summary – today's summary\n"
        "/kill [reason] – STOP everything: block orders, cancel pending, close positions\n"
        "/reset – re-enable trading after a kill (two steps, with a one-time code)")


class CommandHandler:
    def __init__(self, *, allowed_chat_id: int, cfg: AlertsConfig, risk: Any, portfolio: Any, alerts: Alerts,
                 health: Callable[[], Any], code_gen: Callable[[], str] | None = None,
                 mono: Callable[[], float] = time.monotonic):
        self._chat, self.cfg, self._risk, self._pf, self._alerts = allowed_chat_id, cfg, risk, portfolio, alerts
        self._health, self._mono = health, mono
        self._code_gen = code_gen or (lambda: f"{secrets.randbelow(10 ** 6):06d}")
        self._pending_reset: tuple[str, float] | None = None

    # ---- authorisation: the only door into the kill switch from a phone
    def authorised(self, update: dict, now_epoch: float) -> dict | None:
        msg = update.get("message")
        if not isinstance(msg, dict):
            return None
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        if chat.get("type") != "private" or chat.get("id") != self._chat or sender.get("id") != self._chat \
                or sender.get("is_bot"):
            log.warning("Ignored a Telegram message from an unauthorised chat/sender (chat id %s)", chat.get("id"))
            return None
        if now_epoch - float(msg.get("date", 0)) > self.cfg.command_max_age_seconds:
            log.warning("Ignored a stale Telegram command (older than %ss)", self.cfg.command_max_age_seconds)
            return None
        return msg

    def handle_update(self, update: dict, now_epoch: float) -> str | None:
        msg = self.authorised(update, now_epoch)
        if msg is None or not isinstance(msg.get("text"), str):
            return None
        parts = msg["text"].strip().split(maxsplit=2)
        if not parts or not parts[0].startswith("/"):
            return None
        cmd, args = parts[0].split("@")[0].lower(), parts[1:]
        who = (msg.get("from") or {}).get("username") or str((msg.get("from") or {}).get("id"))
        try:
            if cmd in ("/start", "/help"):
                return HELP
            if cmd == "/status":
                return self.status_text()
            if cmd == "/positions":
                return self.positions_text()
            if cmd == "/summary":
                return self._alerts.build_summary(self._alerts._clock().date())
            if cmd == "/kill":
                reason = " ".join(args) or "telegram /kill"
                self._risk.kill(reason, f"telegram:{who}")
                return ("🚨 KILL SWITCH ON. New orders are blocked; pending orders are being cancelled and positions closed.\n"
                        "You will get a message for each fill. Check /status. To re-enable later: /reset")
            if cmd == "/reset":
                return self._reset(args, who)
        except Exception:
            log.exception("command %s failed", cmd)
            return f"The command {cmd} failed with an error. Check the system; use the command line if needed."
        return "Unknown command.\n" + HELP

    def _reset(self, args: list[str], who: str) -> str:
        if not self._risk.status()["kill_active"]:
            return "The kill switch is not on, so there is nothing to reset."
        now = self._mono()
        if not args:
            code = self._code_gen()
            self._pending_reset = (code, now + self.cfg.reset_code_ttl_seconds)
            return (f"To re-enable trading, confirm that positions and the feed look right (/status), then send:\n"
                    f"/reset {code} <why it is safe>\n"
                    f"The code is valid for {self.cfg.reset_code_ttl_seconds:.0f} seconds.")
        code, note = args[0], " ".join(args[1:]).strip()
        if self._pending_reset is None or now > self._pending_reset[1] or not hmac.compare_digest(code.encode(), self._pending_reset[0].encode()):
            self._pending_reset = None
            return "That code is wrong or has expired. Send /reset again to get a new one."
        if len(note) < 5:
            return "Please add a reason after the code, e.g.  /reset " + code + " checked positions, feed healthy"
        self._pending_reset = None
        self._risk.reset_kill(f"telegram:{who}", note)
        st = self._risk.status()
        extra = "\nNote: the daily loss limit was hit earlier, so entries stay blocked until tomorrow." if st["day_halted"] else ""
        return f"✅ Kill switch reset by {who}. Trading is armed again.{extra}"

    def status_text(self) -> str:
        st, h = self._risk.status(), self._health()
        pnl = st["pnl_today"]
        return "\n".join([
            f"{self._alerts.tag} STATUS {datetime.now(IST):%H:%M:%S}",
            f"Kill switch: {'ON — ' + (st['kill_reason'] or '') if st['kill_active'] else 'off'}",
            f"Day halted: {'YES — ' + st['halt_reason'] if st['day_halted'] else 'no'}",
            f"Feed: {'healthy' if h.healthy else 'UNHEALTHY — ' + h.reason}",
            f"P&L today: {_rs(pnl, True)}   (loss limit {_rs(-st['loss_limit'])})",
            f"Orders {st['orders_used']}/{st['orders_limit']} · positions {st['open_positions']}/{st['positions_limit']}"])

    def positions_text(self) -> str:
        det = self._pf.positions_detail() if hasattr(self._pf, "positions_detail") else []
        if not det:
            return "No open positions."
        out = []
        for p in det:
            legs = "; ".join(f"{l['side']} {l['lots']}× {l['symbol']} {l['entry']:.2f}→{l['mark']:.2f}" for l in p["legs"])
            out.append(f"{p['position_id']} ({p['strategy']}): {legs}\n   open P&L {_rs(p['unrealised'], True)} · stop {p['stop_loss']} · target {p['target']}")
        return "\n".join(out)


class CommandPoller:
    """Long-polls Telegram for your commands and sends back the replies."""

    def __init__(self, api: TelegramApi, handler: CommandHandler, sender: AlertSender, chat_id: int,
                 cfg: AlertsConfig, clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self._api, self._h, self._sender, self._chat = api, handler, sender, chat_id
        self._cfg, self._clock, self._sleep = cfg, clock, sleep
        self._offset: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def drain_backlog(self) -> None:
        """Discard everything Telegram queued while we were not running (see module docstring)."""
        updates = self._api.get_updates(offset=-1, timeout=0)
        if updates:
            self._offset = updates[-1]["update_id"] + 1

    def poll_once(self, timeout: int | None = None) -> int:
        updates = self._api.get_updates(self._offset, self._cfg.command_poll_seconds if timeout is None else timeout)
        for u in updates:
            self._offset = u["update_id"] + 1  # advance BEFORE handling: a crashing command is not retried forever
            reply = self._h.handle_update(u, self._clock())
            if reply:
                self._sender.submit(REPLY, f"{self._h._alerts.tag} {reply}")
        if updates:
            self._sender.drain()  # replies should not wait for the sender thread's next wake-up
        return len(updates)

    def _run(self) -> None:
        try:
            self.drain_backlog()
        except TelegramError as e:
            log.error("Could not clear the Telegram backlog (%s); commands stay OFF to be safe", e)
            return
        failures = 0
        while not self._stop.is_set():
            try:
                self.poll_once()
                failures = 0
            except TelegramError as e:
                failures += 1
                hint = " (another program is using this bot's updates, or a webhook is set on it)" if e.code == 409 else ""
                log.warning("Telegram command polling failed: %s%s", e, hint)
                self._sleep(min(60, 2 ** min(failures, 6)))
            except Exception:
                log.exception("command poller error")
                self._sleep(5)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-commands", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()  # the thread may be inside a long poll; it is a daemon and ends with the process


# ================================================================= assembly

def parse_chat_id(raw: str) -> int | None:
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return None


def build_alerts(settings: Any, cfg: AlertsConfig | None = None, live: bool = False,
                 transport: Callable[[str, dict, float], dict] | None = None) -> tuple[Alerts, TelegramApi | None, int | None]:
    """Returns (alerts, api, chat_id). Alerts is a harmless no-op when Telegram is not set up."""
    cfg = cfg or AlertsConfig()
    token = settings.telegram_bot_token.get_secret_value()
    chat_id = parse_chat_id(settings.telegram_chat_id)
    if not cfg.enabled or not token or chat_id is None:
        why = ("disabled in alerts.yaml" if not cfg.enabled else "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env"
               if not token or not settings.telegram_chat_id else "TELEGRAM_CHAT_ID must be a number")
        log.warning("Telegram alerts are OFF (%s). Trading is unaffected.", why)
        return Alerts(None, cfg, live), None, None
    api = TelegramApi(transport or urllib_transport(token))
    return Alerts(AlertSender(api, chat_id, cfg), cfg, live), api, chat_id


def attach_commands(alerts: Alerts, api: TelegramApi, chat_id: int, *, risk: Any, portfolio: Any,
                    health: Callable[[], Any]) -> CommandPoller | None:
    if not alerts.cfg.accept_commands or alerts._sender is None:
        return None
    handler = CommandHandler(allowed_chat_id=chat_id, cfg=alerts.cfg, risk=risk, portfolio=portfolio, alerts=alerts, health=health)
    return CommandPoller(api, handler, alerts._sender, chat_id, alerts.cfg)


# ====================================================================== CLI

def _cli(argv: list[str] | None = None, transport: Callable[[str, dict, float], dict] | None = None,
         out: Callable[[str], None] = print) -> int:
    """
        python -m trader.alerts test      check the token and send a test message
        python -m trader.alerts whoami    find your chat ID (message your bot first)
    """
    ap = argparse.ArgumentParser(prog="python -m trader.alerts")
    ap.add_argument("cmd", choices=["test", "whoami"])
    args = ap.parse_args(argv)

    from .config import load_settings
    s = load_settings()
    token = s.telegram_bot_token.get_secret_value()
    if not token:
        out("TELEGRAM_BOT_TOKEN is not set. Put it in your .env file (never in chat or git). See docs/telegram.md.")
        return 2
    api = TelegramApi(transport or urllib_transport(token))
    try:
        me = api.get_me()
        out(f"Token OK: your bot is @{me.get('username')}")
        if args.cmd == "whoami":
            seen: dict[int, str] = {}
            for u in api.get_updates(None, 0):
                m = u.get("message") or {}
                c = m.get("chat") or {}
                if c.get("type") == "private":
                    f = m.get("from") or {}
                    seen[c["id"]] = f"{f.get('first_name', '')} (@{f.get('username', 'no username')})".strip()
            if not seen:
                out("No messages found. Open Telegram, send any message (e.g. 'hi') to your bot, then run this again.")
                return 1
            for cid, name in seen.items():
                out(f"Chat ID {cid}  —  {name}")
            out("Put your chat ID in .env as TELEGRAM_CHAT_ID=<number>, then run:  python -m trader.alerts test")
            return 0
        chat = parse_chat_id(s.telegram_chat_id)
        if chat is None:
            out("TELEGRAM_CHAT_ID is missing or not a number. Run:  python -m trader.alerts whoami")
            return 2
        api.send_message(chat, "✅ Test message from your options-trading system. Alerts are working. (/help for commands)")
        out(f"Sent a test message to chat {chat}. Check your phone.")
        return 0
    except TelegramError as e:
        out(f"Telegram problem: {e}")
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(_cli())
