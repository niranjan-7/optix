# Backtest guide (read the limits first)

A backtest **replays prices the system recorded earlier** through the same strategy, risk engine and paper
broker it uses live, so you can ask "what would these rules have done on those days?". It also compares
the replay with what paper trading actually did, which tells you how far to trust the replay at all.

> **A backtest is a story about the past, told with a simplified model. It cannot tell you a strategy
> will make money.** Markets are risky and most retail F&O traders lose money. A good backtest is a reason
> to keep paper trading. It is never a reason to go live.

---

## The limits, in plain words

| Limit | What it means for you |
|---|---|
| **Sampled data, not every tick** | The system saves the option chain every 5 seconds, not every price change. In the replay, entries happen a little later than they would live. A stop-loss or target can be **overshot or missed between two samples**. Orders are only checked for fills at sample times, so fills can be up to one interval later than live. |
| **No real slippage** | Fills use the paper model: the quoted price plus or minus one tick. There is no queue, no market impact, no partial fill, no outage. In a fast market real fills are worse, sometimes much worse. |
| **Fees are an estimate** | Brokerage only, unless you add more in `config/execution.yaml`. STT, exchange fees and GST are not included by default. |
| **Overfitting** | If you keep adjusting the rules until the backtest looks good, you are fitting the noise of those particular days, not discovering anything real. See the next section. |
| **Your recording is the limit** | A gap in the recording (the program was off, the feed was down) is a gap in the test. A strike that was outside the recorded window at that moment cannot be traded in the replay. |
| **Today's code and limits** | The replay uses the rules and risk limits as they are *now*, not as they were on those days. |
| **Small samples are mostly luck** | Under about 20 recorded days and about 30 trades, the numbers say almost nothing. The report warns you every time. |

---

## Not fooling yourself (overfitting)

1. **Change one thing at a time**, and write down what and why (the experiments log, module 13).
2. **Keep every variant as a file in git** (for example `config/strategy_move40.yaml`) so you can always see
   exactly what was tested. The backtest only accepts rules from a file, on purpose.
3. **Judge a change on days you did not use to design it.** Use `--split`: pick a date, design the idea on the
   days before it, and look at the days after it. If it only works *before* the split, it is almost certainly noise.
4. **Be suspicious of success.** If a small tweak turns a loss into a big win, assume luck first.
5. **Compare with paper trading** (below). Paper trading on *new* days is the real test; a backtest is only a filter.

---

## How to use it

### 1. Record data

Run the system during market hours. It records a snapshot of the whole subscribed option chain every
5 seconds into `data/market.db`, together with what each contract is (strike, expiry, lot size), so old
data stays usable after those contracts expire. Check what you have:

```bash
python -m trader.journal snapshots
```

### 2. Replay a date range

```bash
python -m trader.backtest run --from 2026-11-02 --to 2026-11-13
```

It prints the limits first, then a summary in the same format as the weekly report but titled
**BACKTEST SUMMARY (SIMULATED replay, not real trading)**, then data-quality warnings (gaps, thin days,
positions still open when the data ended).

### 3. Check it on days you did not design for

```bash
python -m trader.backtest run --from 2026-11-02 --to 2026-11-13 --split 2026-11-09
```

It reports "before" and "after" the split date separately.

### 4. Try a rule variant

```bash
python -m trader.backtest run --from 2026-11-02 --to 2026-11-13 --strategy-config config/strategy_move40.yaml
```

### 5. Compare with what paper trading really did

```bash
python -m trader.backtest run --from 2026-11-02 --to 2026-11-13 --paper-journal data/journal.db
```

Trades are matched by their entry (strategy, contract and minute). You see which trades appear in both,
which only in the replay, which only in paper trading, and the difference in P&L. **Some difference is
normal** (the replay sees samples, paper trading saw every tick). Large, persistent differences mean the
replay is not a faithful model and its numbers should not be trusted.

Add `--csv trades.csv` to save the replay's trades for Excel.

---

## Safety

- A backtest can only ever use the **paper** broker. It has no code path to a real one, and needs no credentials.
- Everything it writes (risk state, journal) goes into a throw-away folder. It never touches your real
  `data/` folder, your real kill switch, or your real journal, and sends no Telegram messages.
- It cannot see the future: it processes snapshots strictly in time order, and a test checks that cutting the
  recording short cannot change anything that happened before the cut.

---

## What was and was not verified

Verified by tests: a recorded session replays into **exactly the same trades** that live paper trading made
from the same price stream (entries, exits, prices, P&L), across two days including a position held overnight.

Not verified: any claim about real markets. The tests use a synthetic price path. How faithful the replay is
on your real recordings is exactly what step 5 above measures.
