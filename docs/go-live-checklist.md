# Go-live checklist

**Going live means real orders with real money.** Markets are risky and most retail F&O traders lose money; this
software has never been run against the real broker. This checklist exists to slow you down. **Every box must be
ticked, honestly, before you set `LIVE_TRADING=true`.** If you are unsure about a box, it is not ticked.

> The system enforces some of these itself (two-key start-up, daily loss stop, mandatory stop-loss). It cannot
> enforce the human ones. Those are on you.

---

## A. Evidence from paper trading

- [ ] **At least 4 full weeks (20 trading days) of paper trading** with the real price feed (not simulated), the
      same rules and limits you intend to use live, running the whole time, not just now and then.
- [ ] **Every Friday's weekly summary was read** (`scripts/summary.sh`) and written up in `config/EXPERIMENTS.md`.
- [ ] **The evening review was done** for (nearly) every trade. You can say how many trades followed the rules and
      what happened when they did not.
- [ ] **You understand the results.** You can explain in your own words: the win rate, the average win versus the
      average loss, the biggest drawdown, and what the fees and slippage cost.
- [ ] **You have at least 30 closed trades.** Under that, the numbers are mostly luck. (If the strategy trades rarely,
      keep paper trading longer.)
- [ ] **You accept that paper results are optimistic.** Real fills are worse (no queue, no market impact in
      paper). If the paper result is only slightly positive, assume the real result is negative.
- [ ] **No rule was tuned to the results** (overfitting). Every change is in `CHANGELOG.md` with a reason written
      *before* you saw the outcome.

## B. Risk limits (`config/risk.yaml`)

- [ ] **You can afford to lose the daily loss limit every day for a week** without it hurting your life. If not,
      lower it.
- [ ] **`max_loss_per_trade` and `max_loss_per_day` are your own numbers**, not the defaults you never thought about.
- [ ] **Minimum size only:** `max_lots_per_order: 1`, and the first live trades are **one lot**.
- [ ] **`max_open_positions`, `max_orders_per_day`** are small and understood.
- [ ] **Only simple structures are enabled** (long calls/puts). Do not enable spreads for the first live weeks.
- [ ] **The loss limits are less than the money in the account** by a wide margin (remember margin and fees).

## C. The broker (Angel One)

- [ ] **API access works** with real credentials: `python -m trader` (live prices, paper broker) ran for days with
      a healthy feed.
- [ ] **A static IP is registered with Angel One** for placing orders through the API (their rules require it for API
      orders; check their current requirements). The computer or server must actually use that IP.
- [ ] **TOTP, PIN and API key** are in `.env` only, and `.env` is not in git, a chat, a screenshot or a shared drive.
- [ ] **Funds and permissions:** the account is enabled for index options (NFO) and has the intended capital.
- [ ] **You know how to square off by hand** in the Angel One app, and the app is installed and logged in on your phone.

## D. Safety nets that must work

- [ ] **Kill switch tested all three ways** while paper trading: dashboard button, Telegram `/kill`, command line
      (`scripts/kill.sh`). Each time: positions closed, entries blocked, and you could reset it (`/reset` or
      `scripts/reset.sh`).
- [ ] **Telegram alerts are working** (you got signal, fill, refusal, kill and daily-summary messages), and `/status` answers.
- [ ] **The dead man's switch is working:** stop the program on purpose and confirm you get a message from the
      watchdog service within a few minutes.
- [ ] **You have seen the "market data down" alert** (unplug the internet briefly in paper mode) and know what to do.
- [ ] **You know what the dashboard looks like when it is unhealthy** (red banners, faded numbers).
- [ ] **The daily loss halt works:** you have seen, in paper mode, trading stop at the loss limit (lower the limit
      temporarily to test it).
- [ ] **Backups:** `data/journal.db` is copied somewhere else regularly.

## E. The first live day

- [ ] **Someone (you) is at the keyboard and watching for the whole session** on the first live days, with the
      Angel One app open next to the dashboard.
- [ ] **Run the first live order as a deliberate 1-lot test**, then **compare the Angel One app with the dashboard**:
      order, price, quantity, status. They must agree. *The live order code has never run against the real
      broker, so this step is what verifies it.* If anything looks different, press the kill switch, close by hand,
      and stop.
- [ ] **Check the first fill's price against the quote**: is the slippage reasonable?
- [ ] **Start-up is attended:** `LIVE_TRADING=true` in `.env`, then start the program yourself in a terminal where
      you can type the confirmation phrase (it refuses to start live mode unattended):
      ```bash
      scripts/stop.sh        # stop the normal (paper) container first, so port 8000 is free
      docker compose run --rm --service-ports trader python -m trader --host 0.0.0.0 --trust-proxy-from-any-peer
      ```
      Type the phrase it shows. The banner must say **LIVE TRADING** in red on the dashboard.
- [ ] **It does not restart live mode by itself.** After a reboot or the 08:30 restart, live mode stays OFF until
      you start it again by hand. That is deliberate.

## F. How you will behave

- [ ] **You will not "make up" a loss** by raising the size or removing a limit after a bad day.
- [ ] **You will not turn off the kill switch, daily stop or stop-losses** because they "got in the way".
- [ ] **You have a rule for when to stop** (for example: stop if the account falls by a set amount, or after a set
      number of losing days) and you wrote it down *before* starting.
- [ ] **You will keep doing the evening review** with real money, and keep the weekly review.

---

## If you tick every box

You are allowed to try, **with one lot**, with money you can lose entirely. Expect it to be harder than paper
trading. Review again after the first week and the first month. There is no shame in going back to paper.

## If you are unsure about any box

You are not ready. Keep paper trading. Nothing is lost by waiting.
