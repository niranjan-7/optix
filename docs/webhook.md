# Webhook guide

The webhook lets an outside tool (for example a TradingView alert) send a trade idea to this system.
**It is a suggestion box, not a remote control:** every message goes through the same risk engine as
the built-in strategy, and the risk engine can say no. A message can only **buy** (open a long
option) or **exit** (close one). It cannot sell to open, so it cannot create a naked short.

> This is paper trading unless you have deliberately gone live (see the go-live checklist in the
> README). Markets are risky and most retail F&O traders lose money. Nothing here is a
> recommendation to trade.

---

## 1. One-time setup

1. Make a secret (16+ characters) and put it in `.env`:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   ```
   WEBHOOK_SECRET=paste-it-here
   ```

   The server **refuses to start** if the secret is missing or short.
2. Start the system. For now this runs on simulated prices, in paper mode:

   ```bash
   .venv/bin/python -m trader.webhook
   ```

   It prints a ready-to-paste `curl` test command. The server listens only on your own computer
   (`127.0.0.1:8000`); the tunnel (section 5) is what makes it reachable from the internet.

> Entries are only accepted inside the trading window in `config/risk.yaml` (Mon-Fri, 09:20-15:00 IST
> by default). Outside it, a valid message is answered with `"status": "rejected"` and the reason
> `OUTSIDE_WINDOW`. That is the safety gate working, not a fault.

---

## 2. The message

Send a `POST` to `/webhook` with a JSON body.

| Field | Required | Meaning |
|---|---|---|
| `idempotency_id` | yes | A **unique** id for this alert, 8-64 characters from `A-Z a-z 0-9 . _ : -`. The same id can never be used twice (a repeat is rejected as `DUPLICATE`). This protects you from a message being delivered twice. |
| `symbol` | yes | `"NIFTY"` |
| `action` | yes | `"BUY"` = open a long option. `"EXIT"` = close it. Exactly these, in capitals. `"SELL"` is refused. |
| `strike` | yes | e.g. `25000` (must be a listed strike) |
| `option_type` | yes | `"CE"` or `"PE"` |
| `qty` | yes | Quantity in **units, not lots**. Must be a multiple of the lot size: NIFTY lot = **65**, so one lot is `65`. (`1` is refused with an explanation, so you can never accidentally send 65 lots.) |
| `stop_loss` | BUY | Absolute **option price** at which to exit with a loss, e.g. `75.2` |
| `target` | no | Absolute option price at which to take profit |
| `stop_loss_pct` | BUY | Instead of `stop_loss`: percent **below** the entry price, e.g. `25`. Resolved from the live quote. |
| `target_pct` | no | Instead of `target`: percent **above** the entry price, e.g. `40` |
| `expiry` | no | `"2026-11-17"`. Default: the nearest expiry, but not expiry day itself. |
| `timestamp` | no | If sent (ISO format), the message is refused when it is more than 120 s from now. This stops old captured messages being replayed. |
| `secret` | see below | The shared secret, for senders that cannot set headers. |

A `BUY` **must** carry a stop-loss (`stop_loss` or `stop_loss_pct`); without one the risk engine
rejects it (`NO_STOP_LOSS`). Give a level either as an absolute price or as a percentage, not both.
An `EXIT` carries no stop or target.

Unknown fields, wrong types (`"65"` in quotes instead of `65`), `NaN`, and unlisted strikes are all
refused with a message naming the problem.

### What you get back

| HTTP | Meaning |
|---|---|
| `200` + `"status": "approved"` | The risk engine approved it and order placement has started. It does **not** mean the order has filled. |
| `200` + `"status": "rejected"` + `reasons` | The risk engine said no. The reasons say why (e.g. `MAX_LOSS_PER_TRADE`, `FEED_UNHEALTHY`, `DUPLICATE`, `KILL_SWITCH`). Nothing was ordered. |
| `401` | Wrong or missing secret / signature. |
| `413` | Body larger than 4 KB. |
| `400` / `422` | The message is malformed, or names a contract that does not exist. The `detail` says what to fix. |
| `429` | Too many requests, or too many wrong secrets from your address. `Retry-After` says how long to wait. |

---

## 3. Authenticating

**Option A: header (curl, scripts).**

```bash
curl -s -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{"idempotency_id":"manual-0001","symbol":"NIFTY","action":"BUY","strike":25000,
       "option_type":"CE","qty":65,"stop_loss_pct":25,"target_pct":40}'
```

**Option B: secret inside the message (TradingView).** TradingView can send a message body but
**cannot add custom headers**, so put the secret in the body as `"secret"`. The server removes it
before processing. (Turn this off with `allow_body_secret: false` in `config/webhook.yaml` if you only
use headers.)

**Option C: HMAC signature (extra protection, for scripts).** Set `WEBHOOK_HMAC_SECRET` in `.env`.
Then every request must also carry `X-Signature: sha256=<hex>`, an HMAC-SHA256 of the exact body bytes:

```bash
BODY='{"idempotency_id":"manual-0002","symbol":"NIFTY","action":"EXIT","strike":25000,"option_type":"CE","qty":65}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_HMAC_SECRET" | sed 's/^.* //')"
curl -s -X POST http://127.0.0.1:8000/webhook \
  -H "Content-Type: application/json" -H "X-Webhook-Secret: $WEBHOOK_SECRET" -H "X-Signature: $SIG" \
  -d "$BODY"
```

Changing even one character of the body invalidates the signature. TradingView cannot compute
signatures, so leave `WEBHOOK_HMAC_SECRET` empty if TradingView is your only sender.

Five wrong secrets in a minute from one address blocks that address for ten minutes, even if it then
uses the right secret.

---

## 4. TradingView

**Requirements (check TradingView's current rules, they change):** webhook notifications need a paid
TradingView plan and two-factor authentication turned on. TradingView only sends to standard web
ports (80/443), which the tunnel provides, and it gives up if your server takes more than a few
seconds to answer.

1. Open the chart and create an alert (the alarm-clock icon).
2. On the **Notifications** tab, tick **Webhook URL** and paste your public URL:
   `https://hooks.yourdomain.com/webhook` (see section 5).
3. In the **Message** box, paste one of these. Use **one alert per action**.

**Open a position (BUY):**

```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "idempotency_id": "buy-{{ticker}}-{{timenow}}",
  "timestamp": "{{timenow}}",
  "symbol": "NIFTY",
  "action": "BUY",
  "strike": 25000,
  "option_type": "CE",
  "qty": 65,
  "stop_loss_pct": 25,
  "target_pct": 40
}
```

**Close it (EXIT):**

```json
{
  "secret": "YOUR_WEBHOOK_SECRET",
  "idempotency_id": "exit-{{ticker}}-{{timenow}}",
  "symbol": "NIFTY",
  "action": "EXIT",
  "strike": 25000,
  "option_type": "CE",
  "qty": 65
}
```

Things to know:

- **Unique ids.** `{{ticker}}` and `{{timenow}}` are TradingView placeholders that make each firing
  unique. A fixed id would be accepted once and then rejected as `DUPLICATE` forever.
- **The strike is fixed in the message.** A TradingView alert cannot pick "the at-the-money strike
  today". If you want that, the built-in strategy is the better tool, or you re-edit the alert.
- **Why percentages.** An alert on a NIFTY chart does not know the option's price, so it cannot
  calculate an absolute stop. `stop_loss_pct` lets the server work it out from the live quote.
- **Test it first.** Send an alert while the market is closed: you should get `rejected` with
  `OUTSIDE_WINDOW`. That proves the pipe works end to end without risking anything.
- Check what happened in the system log: every risk decision (approved or rejected, with reasons) and every failed login attempt is logged.

---

## 5. Exposing it with a free Cloudflare Tunnel

A tunnel lets TradingView reach your computer **without opening a port on your router**. Install
`cloudflared` from Cloudflare's website (<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/>).

### Quick test (no account, URL changes every time)

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints an address like `https://something-random.trycloudflare.com`. Your webhook URL is that
address plus `/webhook`. Cloudflare describes these quick tunnels as for testing only: no uptime
promise, and the address changes whenever you restart.

### Permanent setup (needs a free Cloudflare account and a domain on it)

```bash
cloudflared tunnel login
cloudflared tunnel create options-webhook
cloudflared tunnel route dns options-webhook hooks.yourdomain.com
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: options-webhook
credentials-file: /Users/you/.cloudflared/<TUNNEL-ID>.json

ingress:
  # ONLY the webhook path is published. Everything else gets a 404 from Cloudflare itself.
  - hostname: hooks.yourdomain.com
    path: ^/webhook$
    service: http://localhost:8000
  - service: http_status:404
```

Run it with `cloudflared tunnel run options-webhook`. Your webhook URL is
`https://hooks.yourdomain.com/webhook`.

### Security rules for the tunnel

- **Publish only `/webhook`** (the `path:` line above). The dashboard and kill switch must never be
  reachable from the internet. The app has a second line of defence: any request that arrives with
  Cloudflare's headers can only reach `/webhook` and `/health`; every other page answers 404.
- **Never open a port on your router** and never bind the server to `0.0.0.0` on a machine that is
  reachable from the internet. Keep `host: 127.0.0.1` in `config/webhook.yaml`. (In Docker, publish
  the port as `127.0.0.1:8000:8000`.) The "who is calling" information in the logs is only
  trustworthy because the tunnel is the only way in.
- Keep the secret in `.env` only. Do not paste it into chats, screenshots, or git.
- **If the secret may have leaked:** change `WEBHOOK_SECRET` in `.env`, restart, and update your
  alerts. Then use `python -m trader.risk kill` if anything unexpected happened, and review the log.

---

## 6. Troubleshooting

| You see | Likely cause |
|---|---|
| `401 unauthorized` | Secret wrong, missing, or (if HMAC is on) signature wrong. The server deliberately does not say which. |
| `422` ... `action must be exactly "BUY"...` | Lower-case `buy`, or `SELL`. Use exactly `BUY` or `EXIT`. |
| `422` ... `qty is in UNITS...` | You sent lots. NIFTY: `65` = 1 lot, `130` = 2 lots. |
| `422` ... `No such option` | That strike or expiry is not listed. Check the strike is a multiple of 50 and the expiry exists. |
| `422` ... `no live quote` | A `*_pct` level needs a live price and the contract is not currently subscribed. Send an absolute `stop_loss`. |
| `rejected` / `OUTSIDE_WINDOW` | Market closed, weekend, or after 15:00. |
| `rejected` / `FEED_UNHEALTHY` | No fresh market data. See the runbook for what to do. |
| `rejected` / `MAX_LOSS_PER_TRADE` | The stop is too wide for the loss limit in `risk.yaml`. With a 65-unit lot, a 25% stop on a Rs100 option already risks about Rs1,600. |
| `rejected` / `KILL_SWITCH` | The kill switch is on. Entries stay blocked until someone resets it by hand. |
| `rejected` / `DUPLICATE` | That `idempotency_id` was already used. |
| `429` | Slow down (20 per minute, bursts of 5), or you were blocked after wrong secrets. |

---

## 7. What is *not* verified

- The tunnel and TradingView steps above follow those products' documentation as I understand it;
  I could not test them from here (no internet-facing machine, no TradingView account). Treat the
  first real alert as a test, with the market closed.
- Only the `curl` path was exercised against a running server, on simulated prices.
