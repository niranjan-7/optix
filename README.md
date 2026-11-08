# NIFTY Options Trading Assistant

**An open-source, paper-first toolkit for learning to trade NIFTY options with Angel One's SmartAPI.**

It watches option prices, proposes trades from simple rules you can edit, checks every trade against strict safety
limits, records everything in a journal, and alerts your phone. It starts in **paper mode: no real orders, no real
money.** It is built to help people *learn*, to make good habits easy (stop-losses, position limits, an evening
review), and to show how a trading system can be built with safety first. It is free to use, study and change.

> **New here? Start with the Beginner's Guide:** [PDF](docs/Beginners-Guide.pdf) · [Markdown](docs/user-guide.md).
> It walks you from downloading the project, to creating your Angel One API key, to your first paper trade.

[![tests](../../actions/workflows/tests.yml/badge.svg)](../../actions/workflows/tests.yml)

## Why this exists

Most retail F&O traders lose money, often through preventable mistakes: no stop-loss, oversized positions, chasing
losses, trading on bad data, not keeping a record. Most "algo trading" examples online skip the safety. This project
puts the safety first and keeps everything **readable and auditable**, so you can see exactly why every trade was
taken or refused.

## What you can do with it

- **Learn** how an options trading system fits together: prices, Greeks, rules, risk checks, orders, a journal.
- **Practise** with real market prices and a paper broker, with no money at risk.
- **Edit the rules** in plain text files, and keep a record of every change and experiment.
- **Review** your trading honestly: weekly summaries, and an evening review of whether each trade followed the rules.
- **Replay** recorded days (with the limits of backtesting spelled out).
- **Connect TradingView alerts** (optional) through the same safety checks.
- **Extend it:** add strategies, underlyings, or other brokers. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Status: early (alpha). Read the honest part

- About 670 automated tests pass, and the Docker setup has been run end to end on simulated prices.
- **The live Angel One connection and real order placement have never been run against the real servers.** They are
  written to Angel One's documented formats and need a supervised 1-lot test before anyone relies on them.
  See [What has and has not been verified](#what-has-and-has-not-been-verified).
- **Nothing here is a trading strategy you can rely on.** The included example strategy is "example only, not tested
  for profit". The project makes no claim that any rule set is, or will be, profitable.

## Independent project

This is an independent, community project. It is **not affiliated with, endorsed by, or supported by Angel One**,
TradingView, Telegram or Cloudflare. Their names are trademarks of their owners. "SmartAPI" is Angel One's API; this
project simply uses it.

## Quick start (about 10 minutes, no account needed)

You need [Git](https://git-scm.com/downloads) and [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
git clone https://github.com/<your-account>/<this-repo>.git
cd <this-repo>
cp .env.example .env          # then edit .env: set WEBHOOK_SECRET and TRADER_FEED=simulated (see the guide)
scripts/start.sh              # Windows: docker compose up -d --build
```

Open **http://127.0.0.1:8000/**. You will see the dashboard on made-up prices. The
[Beginner's Guide](docs/user-guide.md) covers everything else, including real prices from Angel One.

## Contributing, security, license

- Contributions are welcome: [CONTRIBUTING.md](CONTRIBUTING.md) (including the safety rules a change must never break).
- Security issues: please report privately, see [SECURITY.md](SECURITY.md).
- Be kind: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- License: [MIT](LICENSE). The software is provided as is, with no warranty and no liability.

---

## ⚠️ Read this first

**Markets are risky. Most retail F&O traders lose money.** Studies of Indian retail futures-and-options
traders have repeatedly found that the large majority lose money over time. Nothing in this project is
investment advice, and nothing in it promises or implies that any strategy is, or will be, profitable.

- The example strategy is **"example only, not tested for profit."** It exists to show how the system works.
- A good result on paper, or in a backtest, does **not** mean a good result with real money. Real fills are
  worse, and past results do not predict the future.
- You can lose **all** the money you put at risk, and the safety limits reduce losses but cannot remove them.
- Only ever trade money you can afford to lose completely.
- Software has bugs. This one is tested, but it has **not** been run against the real Angel One broker or
  with real money. See "What has and has not been verified" at the end.

---

## What it does, in plain words

1. **Watches** NIFTY option prices (real ones from Angel One, or made-up ones for practice).
2. **Proposes** trades. Either from a rule file (`config/strategy.yaml`) or from a TradingView alert (webhook).
3. **Checks every proposal** against your safety limits (`config/risk.yaml`): maximum loss per trade and per
   day, number of positions, trading hours, a mandatory stop-loss, and more. **Nothing reaches the broker
   without passing this check. By default it can only buy options or close them; even if you later enable spreads, every option it sells must be covered by one it also holds, so it can never sell an uncovered ("naked") option.**
4. **Trades on paper**: it pretends to buy and sell, using real prices, and records the results.
5. **Records everything** in a journal: every idea, every decision and the reason, every fill, every trade.
6. **Tells you** on Telegram about everything that happens, and lets you stop everything from your phone.
7. **Teaches you**: a weekly summary and an evening review where you note whether each trade followed the rules.

### The safety design (why you can trust it more than a script)

| Safety feature | What it means |
|---|---|
| **Paper by default** | Real orders need `LIVE_TRADING=true` **and** you typing a confirmation phrase at start-up. A restart can never turn it on. |
| **One gate** | Every trade, whether from the rules or TradingView, goes through the same risk check. |
| **Defined risk only** | Long options (and, if you enable them, spreads where every sold option is covered). Selling an uncovered option is impossible. |
| **Mandatory stop-loss** | A trade without a stop-loss is refused. |
| **Daily loss stop** | When the day's loss reaches your limit, trading stops and positions are closed. |
| **Kill switch** | Stops everything, from the dashboard, Telegram, or the command line. It stays on, even after a restart, until a person resets it. |
| **Bad data blocks trading** | If the price feed is stale or down, no new trades are allowed. |

---

## Setting it up (once)

You need a computer that stays on during market hours (a Mac, Windows PC or Linux machine), **Docker**
(Docker Desktop is free for personal use), and **git**.

### 1. Get the code

Put this folder on your computer (for example `~/options-trading`).

### 2. Create your `.env` file (your secrets)

Copy `.env.example` to `.env` and fill in the blanks. **`.env` holds your passwords. Never share it, never
paste it into a chat or email, and never commit it to git** (it is already ignored).

| Setting | What it is |
|---|---|
| `WEBHOOK_SECRET` | **Required.** A long random password (16+ characters). Make one with: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TRADER_FEED` | `live` (real prices) or `simulated` (made-up prices for practice). |
| `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET` | Your Angel One SmartAPI login. Needed for real prices. (Leave blank when `TRADER_FEED=simulated`.) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Phone alerts and commands. Setup steps: [docs/telegram.md](docs/telegram.md). Strongly recommended. |
| `HEARTBEAT_URL` | A "dead man's switch" so you are told if the program dies (see below). Strongly recommended. |
| `LIVE_TRADING` | **Leave as `false`.** See the go-live checklist. |

### 3. The dead man's switch (recommended)

A crashed program cannot send you an alert. So create a free check at [healthchecks.io](https://healthchecks.io)
(or a similar service), tell it to expect a ping every 1–2 minutes, and paste its ping URL into
`HEARTBEAT_URL`. The program pings it every minute. **If the pings stop, that service messages you.**

### 4. Start it

```bash
scripts/start.sh
```

(or `docker compose up -d --build`). The first time takes a few minutes. Then open
**http://127.0.0.1:8000/** in your browser. You should see the dashboard, a green **PAPER** label, and a
green **FEED healthy** label during market hours.

**Try it without an Angel One account first:** put `TRADER_FEED=simulated` in `.env` (and leave the Angel One lines
blank), then start it as above. Prices are made up, which is fine for learning how everything works. Change it
back to `TRADER_FEED=live` (and fill in the Angel One lines) when you are ready for real prices. Either way,
orders are **paper** orders.

---

## The daily routine

### Every morning (before 09:15)

- [ ] **The system is running.** Open the dashboard (http://127.0.0.1:8000/). The page says **PAPER** and the
      **MODE** pill is green. (It restarts itself at 08:30 each day to pick up the new day's contracts.)
- [ ] **Kill switch is off.** The **RISK** pill says "armed, normal". If it says KILL SWITCH ON, find out why
      before resetting it (see below).
- [ ] **Phone check.** Send `/status` to your bot. You should get a reply. If not, see Telegram troubleshooting.
- [ ] **Computer is awake** and on the internet, and the power is plugged in. Turn off sleep for the day.
- [ ] **No leftover positions** from yesterday (dashboard "Open positions" is empty).

### Through the day

You don't need to watch it. Alerts come to your phone. Glance at the dashboard now and then.
**Entries are only allowed 09:20–15:00; everything is closed by 15:15 (the system forces it at 15:20).**

### Every evening

- [ ] **15:45: daily summary** arrives on Telegram. Check there are **no open positions** left.
- [ ] **Evening review** (10 minutes): for each trade today, did it follow the rules?
      ```bash
      scripts/review.sh "Your Name"
      ```
      Answer **y** or **n** and add a note if something is worth remembering. This is the most valuable habit in
      the whole system: it is how you tell a good rule from luck.
- [ ] Look at any **refused** signals (⛔ on Telegram or the dashboard). Do the reasons make sense?

### Every Friday evening (the weekly review)

```bash
scripts/summary.sh
```

Read the whole report: win rate, average win and loss, biggest drawdown, results by setup, and how
many trades followed the rules. Write what you learned in `config/EXPERIMENTS.md`. **Be honest with
yourself about small samples: under about 30 trades the numbers are mostly luck.**

### Back up your journal (weekly)

Your trade history lives in the `data/` folder (`journal.db` is the precious one). Stop the system
(`scripts/stop.sh`), copy the `data` folder to an external drive or cloud storage, then start it again.

---

## Starting, stopping, restarting

| Do this | Command |
|---|---|
| Start | `scripts/start.sh` |
| Stop (**close positions first**: stopping does not close them) | `scripts/stop.sh` |
| Is it running? | `scripts/status.sh` |
| Watch the log | `scripts/logs.sh` (Ctrl-C to stop watching) |
| Apply changed rules or limits | edit the file, commit it (see "Changing the rules"), then `docker compose restart` |

On **Windows**, run the `docker compose ...` lines inside each script yourself in PowerShell
(for example `docker compose up -d --build`), or use WSL / Git Bash to run the `.sh` scripts.

The system restarts itself at **08:30** every day. It **will not restart while a position is open**; it
tells you and waits.

---

## 🛑 THE KILL SWITCH

**What it does:** blocks all new orders immediately, cancels pending orders, and closes every open position.
It **stays on until a person resets it**, even if the program restarts.

**Three ways to use it. Use whichever is nearest:**

1. **Dashboard:** press the big red **KILL SWITCH** button and confirm.
2. **Telegram:** send `/kill` (optionally with a reason: `/kill something looks wrong`).
3. **Command line:** `scripts/kill.sh` (or `docker compose exec trader python -m trader.risk kill`).

**If in doubt, press it.** The worst case is that a trade you wanted is closed early.

**After a kill, check:** dashboard shows no open positions; the broker app (Angel One) shows no open orders or
positions; you understand *why* you killed it.

**To turn trading back on:**

- **Telegram:** send `/reset`. The bot replies with a one-time code. Send `/reset <code> <why it is safe>`.
- **Command line:** `scripts/reset.sh "Your Name" "why it is safe"` and type `RESET` when asked.

(If the **daily loss limit** was what stopped trading, resetting does not lift that: entries stay blocked until
the next trading day. That is deliberate.)

---

## When something goes wrong

### "Market data down" alert, or FEED shows UNHEALTHY

The system stops taking new trades by itself when prices stop updating. **If you have an open position this
is urgent**, because stop-losses are not being watched while the data is down.

1. **Open position?** Close it: press the dashboard **KILL SWITCH** or send `/kill`. Also check the Angel One
   app and close it there if needed.
2. Check the internet connection and that the computer is awake.
3. Check the log: `scripts/logs.sh`. Look for "login failed" (your Angel One login or TOTP secret is wrong, or the
   computer's clock is wrong) or "could not reach".
4. Wait one minute: it reconnects by itself with increasing waits.
5. Still down? `scripts/stop.sh` then `scripts/start.sh`. If it is a **market holiday** the feed is silent
   and this is normal.
6. Still down after 10 minutes? Leave it off for the day (no open positions!) and look again tomorrow.

### No alerts on your phone for a long time during market hours

Alerts are quiet when nothing happens. But if you are unsure: send `/status`. **No reply** means the program or
your internet is down. If you set up the dead man's switch you will be told within minutes of the program dying.

### The dashboard page says "DISCONNECTED" or "DATA IS STALE" in red

The program stopped answering. Do not trust the numbers on the page. Check the Angel One app for your real
positions. Then `scripts/status.sh` and `scripts/logs.sh`.

### It says "positions do not match the broker"

Only relevant for live trading. **Stop. Do not trade.** Compare the Angel One app to the dashboard, fix the
difference by hand at the broker, and ask for help before restarting.

### The computer restarted or lost power

Docker starts the program again by itself (if "start at login" is on for Docker Desktop). Positions, the
kill switch and the journal are saved to disk. **Check the dashboard and your positions in the broker app.**

### A refusal you don't understand

Each refused signal lists reason codes. The common ones:

| Code | Meaning |
|---|---|
| `OUTSIDE_WINDOW` | Market closed, weekend, or after 15:00 |
| `FEED_UNHEALTHY` | Prices are not fresh |
| `MAX_LOSS_PER_TRADE` | The stop-loss would risk more than your per-trade limit |
| `MAX_STRUCTURE_LOSS` | The option is so expensive that its full premium exceeds your limit |
| `MAX_POSITIONS` / `MAX_ORDERS` | You are at your limit for open positions / orders today |
| `NO_STOP_LOSS` / `BAD_STOP_OR_TARGET` | The stop-loss is missing or on the wrong side |
| `DUPLICATE` | That signal id was already used |
| `KILL_SWITCH` / `DAY_HALTED` | The kill switch is on / the daily loss limit was reached |
| `NOT_DEFINED_RISK` | The trade would leave an uncovered short option. Never allowed. |

> **Why you may see lots of refusals:** NIFTY's lot is 65. A 25% stop on a ₹100 option already risks about
> ₹1,600 per lot, so the ₹2,000-per-trade limit only allows options costing up to about ₹120. A week before
> expiry an at-the-money option costs about ₹200+, so entries are refused. That is the safety limit working.
> Raising the limit is a decision for you, made deliberately (see "Changing the rules").

---

## Changing the rules and limits

Rules (`config/strategy.yaml`) and limits (`config/risk.yaml`) are plain text files. **Changing them is a
deliberate act, not a casual tweak**, because the temptation to tune until the results look good is exactly how
people fool themselves.

1. **Decide in advance** what you expect and how you will judge it (see `config/EXPERIMENTS.md`).
2. **Change one thing at a time.**
3. Edit the file. If you change a value, **raise the `version:` number** at the top of the file.
4. Add an entry to `config/CHANGELOG.md` (what, why, expected effect). A test fails if you bump a version
   without an entry.
5. **Commit it to git** with a clear message, so there is a permanent record: `git add config && git commit -m "risk: raise per-trade limit to 2500 (see CHANGELOG)"`.
6. Apply it: `docker compose restart`.
7. Give it enough days (decided in step 1) before judging it. Then record the result in `EXPERIMENTS.md`.

Times in the files must be in quotes (`"09:30"`), or the value is misread. The program refuses to start with
a clear message if a file has a mistake.

---

## Using TradingView alerts

Alerts from TradingView become trade proposals that go through the **same** risk checks. Setup, the message
format, and a free Cloudflare Tunnel are explained in [docs/webhook.md](docs/webhook.md).

---

## Learning from the data

| Tool | Command |
|---|---|
| Weekly summary | `scripts/summary.sh` (add `--days 30` for a month) |
| Evening review | `scripts/review.sh "Your Name"` |
| Export trades to Excel | `docker compose exec trader python -m trader.journal export --csv /app/data/trades.csv --days 30` (then open `data/trades.csv`) |
| Replay recorded days through the rules | see [docs/backtest.md](docs/backtest.md): **read its limits first** |

---

## Where things are

| Path | What |
|---|---|
| `config/*.yaml` | Rules and limits. **Edit deliberately; commit to git.** |
| `config/CHANGELOG.md`, `config/EXPERIMENTS.md` | The record of every change and experiment |
| `data/journal.db` | Your trade journal. **Back this up.** |
| `data/market.db` | Recorded option prices for backtesting (large; safe to delete) |
| `data/risk_state.json` | The kill switch and daily counters. Do not edit by hand. |
| `data/portfolio.json` | Open positions |
| `.env` | Your secrets. **Never share or commit.** |
| `docs/` | Guides: webhook, Telegram, backtest, go-live checklist |
| `src/trader/` | The program |
| `tests/` | Automated tests (run: `python -m pytest`) |

Command-line tools (run inside the container with `docker compose exec trader ...`, or directly with `python -m`):
`trader.risk` (status, kill, reset), `trader.journal` (summary, trades, review, export, snapshots),
`trader.backtest`, `trader.alerts` (test, whoami).

---

## Going live (real money)

**Do not go live until every item in [docs/go-live-checklist.md](docs/go-live-checklist.md) is ticked.**
The short version:

- ☐ **At least 4 weeks of paper trading**, with the weekly summaries read and the evening reviews done.
- ☐ **Risk limits set** to amounts you can afford to lose, and understood.
- ☐ **Static IP** registered with Angel One for API orders.
- ☐ **Minimum size only:** 1 lot.
- ☐ **Kill switch tested** all three ways, and **Telegram + dead man's switch** working.
- ☐ A **first live test with a single lot** while you watch, checking the broker app against the system.

Turning it on needs **two keys**: `LIVE_TRADING=true` in `.env` **and** typing a confirmation phrase at start-up,
at the keyboard. It can never start unattended in live mode.

---

## What has and has not been verified

Honest status, so you know what you are relying on:

**Tested automatically (about 600 tests):** the risk engine and its limits (including attempts to break them),
the structure rules (no naked shorts), position and P&L maths, the paper broker, order sequencing, the journal and
summary maths, the webhook's security, the dashboard, Telegram handling, and the backtest replay.

**Also checked by hand once, on a Mac with Docker Desktop, using simulated prices:** the Docker image builds; the
container runs as an ordinary user with a read-only filesystem and no extra privileges; its port is reachable from
this computer only; the dashboard, webhook security and health check work; the kill switch pressed with
`scripts/kill.sh` reaches the running program, **survives a restart**, and can be reset; and it stops cleanly.
(Not yet checked: Windows or Linux hosts, or Docker with real Angel One credentials.)

**Never tried against the real thing (needs your credentials and market hours):**

- ☐ **The live Angel One connection** (login, WebSocket prices): written to Angel One's documented formats but
  never run against their servers.
- ☐ **Real order placement** (the live broker adapter): the most important unverified part. The go-live
  checklist requires a supervised 1-lot test.
- ☐ **Telegram with your real bot**, **TradingView alerts** and **the Cloudflare Tunnel**: tested against
  simulations only.
- ☐ **Real market behaviour:** the example strategy, the risk limits and the paper fills have only been shown
  to work mechanically, not to make money. The weeks of paper trading are how you find out.

If you find something that behaves differently from this README, trust the dashboard and the broker app, press
the kill switch if in doubt, and fix the README afterwards.
