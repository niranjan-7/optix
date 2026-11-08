# Experiments log

This is where you record **what you tried, what you expected, and what actually happened.** It is the antidote to
fooling yourself. Most "discoveries" in trading are luck; this log is how you find out which.

## The rules of the game

1. **One change at a time.** If you change two things you will not know which one mattered.
2. **Write the hypothesis and the success test BEFORE you look at results.** "I think X will improve Y, and I will
   call it a success if Z, measured over N days." Not after.
3. **Decide how long to run it in advance** (at least 20 trading days or 30 trades; fewer says almost nothing).
   Do not stop early because it looks good, or bad.
4. **Compare with a baseline**: the same period without the change (paper trading with the old rules, or the backtest).
5. **Test on days you did not design it on.** Use `python -m trader.backtest ... --split DATE`: if it works only
   *before* the split, it is almost certainly noise.
6. **Record failures too.** A log of only wins is a lie.
7. **Every experiment links to a `CHANGELOG.md` entry** (the commit that made the change).

---

## Template (copy this)

```
### #<n>: <short title>
- **Started:** YYYY-MM-DD        **Planned length:** N trading days / N trades        **Ends:** YYYY-MM-DD
- **Change:** (file and value, old -> new)   **CHANGELOG entry:** (date + file + version)   **Git commit:** (hash)
- **Hypothesis:** (what I expect, and why)
- **Success test (decided in advance):** (e.g. "net P&L per trade after fees better than baseline by Rs X AND not worse in the out-of-sample days")
- **Baseline:** (what I am comparing with, and its numbers)
- **Result:** (numbers: trades, win rate, avg win/loss, net P&L, max drawdown, rules-followed %)
- **Out-of-sample check:** (the --split result)
- **Decision:** keep / revert / run longer
- **What I learned:** (honest: could this just be luck? what would change my mind?)
```

---

## Weekly review notes (one short paragraph each Friday)

```
### Week of YYYY-MM-DD
- Trades: N   Net P&L: Rs X   Win rate: Y%   Max drawdown: Rs Z   Rules followed: A of B reviewed
- What went well / badly:
- Anything the system refused that surprised me:
- Anything I did by hand that I should not have:
- One thing to look at next week (NOT to change yet):
```

---

## Experiments

(none yet: the first weeks are for watching the baseline strategy, not changing it)

## Weekly notes

(none yet)
