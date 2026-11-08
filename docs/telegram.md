# Telegram alerts and phone commands

Telegram gives you two things: **alerts** on your phone for everything the system does, and a few
**commands** you can send back (most importantly `/kill`).

The system works fine without Telegram. If it is not set up, the program logs one line saying
"Telegram alerts are OFF" and carries on.

> This is paper trading unless you have deliberately gone live. Alerts say `[PAPER]` or `[LIVE]` at the
> start so you can never mistake which one you are looking at.

---

## 1. One-time setup (about five minutes)

### Step 1: create your bot

1. Open Telegram and search for **@BotFather**. Make sure it has the blue verified tick.
2. Send `/newbot`.
3. Give it a display name (anything, e.g. "My Trading Alerts") and then a username, which must end in
   `bot` (e.g. `uncle_trading_alerts_bot`).
4. BotFather replies with a **token** that looks like `123456789:AAH...`.

**Treat that token like a password.** Anyone who has it can send messages as your bot.
**Do not paste it into chat, email, a screenshot, or git.**

### Step 2: put the token in your `.env` file

Open the `.env` file in the project folder (create it by copying `.env.example` if it does not exist)
and fill in just the token line:

```
TELEGRAM_BOT_TOKEN=123456789:AAH...your-token...
```

`.env` is ignored by git, so it never gets published.

### Step 3: say hello to your bot

In Telegram, open your new bot (search its username) and press **Start** (or send "hi").
A bot cannot message you first, so this step is required.

### Step 4: find your chat ID

```bash
python -m trader.alerts whoami
```

It prints something like `Chat ID 123456789 — Uncle (@uncle_username)`. Copy the number into `.env`:

```
TELEGRAM_CHAT_ID=123456789
```

This is the **only** chat that will ever be allowed to give commands.

### Step 5: check it works

```bash
python -m trader.alerts test
```

You should see `Token OK: your bot is @...` and get a test message on your phone. The command never
prints the token.

### Step 6: start the system

When the system starts you will get "System started (PAPER mode...)". From then on you get the alerts
below.

---

## 2. What you will receive

| Alert | When |
|---|---|
| ✅ **SIGNAL APPROVED** | The risk engine accepted a signal. Shows the contract, stop, target and the loss if the stop is hit. |
| ⛔ **SIGNAL REFUSED** | The risk engine said no, with every reason. These are your "rule violations". Identical refusals in a minute are folded into one message. |
| 📤 **ORDER** / ✔️ **FILL** | An order was placed, and each fill (price and fee). |
| 🏁 **POSITION CLOSED** | A trade ended, with its P&L after fees. |
| 🚨 **KILL SWITCH ON** | Someone (you, the dashboard, a command, or the system) switched it on, and why. |
| 🚨 **Daily loss limit hit** | Trading is halted for the day and positions are being closed. |
| ⚠️ / 🚨 **Market data down** | No fresh prices for 15 seconds during market hours (reminder every 5 minutes). It is **critical** if you have open positions, because stop-losses are not being watched. It stays silent overnight and on weekends, when quiet is normal. |
| ⚠️ **Warnings** | Anything the system wants a person to look at, for example an order that could not be priced, or a fill it could not record. |
| 📊 **DAILY SUMMARY** | At 15:45 on trading days: signals, trades, P&L, and how many trades still need your evening review. A quiet day still gets one, as a sign the system is alive. |

You can switch off the routine ones (signals, refusals, orders, fills) in `config/alerts.yaml`.
The safety messages (kill switch, loss limit, feed trouble, warnings, the summary) cannot be switched off.

If a very busy minute produces too many routine messages, some are held back and the next message says
how many. Warnings, critical alerts and replies to your commands are never held back.

---

## 3. Commands

Send these to your bot, from your own phone.

| Command | What it does |
|---|---|
| `/status` | Kill switch, daily halt, feed health, P&L, limits used. |
| `/positions` | Open positions with entry, current price and P&L. |
| `/summary` | Today's summary on demand. |
| `/kill` or `/kill reason` | **Emergency stop.** Blocks all new orders, cancels pending orders, closes every position. One step, takes effect immediately. |
| `/reset` | Re-enable trading after a kill. **Two steps** (below). |
| `/help` | The list. |

### Turning trading back on after a kill

`/reset` alone does nothing except give you a one-time code:

```
You:  /reset
Bot:  To re-enable trading ... send:  /reset 483920 <why it is safe>
You:  /reset 483920 checked positions and the feed, all clear
Bot:  ✅ Kill switch reset by uncle. Trading is armed again.
```

The code works once and expires after two minutes. The reason is recorded with your name. This is
deliberate friction: re-enabling trading should never be a stray tap. (If the *daily loss limit* was
what stopped trading, `/reset` does not lift that; entries stay blocked until the next trading day.)

---

## 4. How it is kept safe

- **Only you can command it.** Messages are obeyed only if they come from your private chat with your
  exact chat ID, from a human (not another bot). Anyone else who finds your bot gets **no reply at all**,
  so it does not even confirm that it exists. Do not add the bot to a group: group messages are ignored.
- **No replays.** Telegram keeps messages you sent while the program was off. Obeying those later would
  be dangerous (an old `/kill` or `/reset`), so on start-up the whole backlog is thrown away, and any
  message more than a minute old is ignored.
- **The token stays secret.** It is only ever used in the web address of the request. It is never written
  to the log, and error messages are stripped of it.
- **Telegram never slows trading.** Messages are sent from a background queue. If Telegram or your internet
  is down, alerts are dropped and logged; trading and the kill switch are unaffected.
- **Plain text only.** Nothing in a message can be read as formatting or a link.

**Privacy:** Telegram bot messages are not end-to-end encrypted, so Telegram can read them. Alerts contain
your trade details and P&L (never passwords or tokens). If that bothers you, switch off the routine alerts
in `config/alerts.yaml` and keep only the safety ones.

**If the token is ever exposed:** in Telegram open @BotFather, send `/revoke`, choose your bot, and put the
new token in `.env`. The old one stops working immediately.

---

## 5. Troubleshooting

| You see | What to do |
|---|---|
| Log line `Telegram alerts are OFF (... not set in .env)` | Fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then restart. |
| `Telegram said: Unauthorized` | The token is wrong or was revoked. Copy it again from BotFather. |
| `Telegram said: Forbidden: bot can't initiate conversation` or `chat not found` | You have not pressed Start on your bot yet (Step 3), or the chat ID is wrong. Run `whoami` again. |
| `whoami` says "No messages found" | Send "hi" to your bot first, then run it again. |
| `Conflict: terminated by other getUpdates request` | Another program (or a second copy of this one) is using the same bot. Stop it. |
| Commands do nothing | Check the log for "ignored a Telegram message from an unauthorised chat": your `TELEGRAM_CHAT_ID` does not match the chat you are typing in. Messages older than a minute are ignored too. |
| Alerts say "(+N routine alerts held back...)" | A busy moment; the routine messages were rate-limited. Everything is still in the journal. |

---

## 6. What is *not* verified

Everything above was tested against a simulated Telegram. It has **not** been tried with a real bot,
because that needs your token. The first real run (`python -m trader.alerts test`) is the real test.
The format of Telegram's replies, including its error messages, was checked against Telegram's live API
using a deliberately invalid token.
