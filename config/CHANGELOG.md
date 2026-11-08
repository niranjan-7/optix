# Changelog: rules and limits

**Every change to a file in this folder gets an entry here, in the same git commit.** This is your permanent
record of what you changed and why, so that in three months you can tell which change helped and which was luck.

How to add one (newest at the top):

1. Change the file and **raise its `version:` number** by one.
2. Add an entry below using the template. Write the **why and the expected effect BEFORE you see the results.**
3. `git add config && git commit -m "<file>: <what> (see CHANGELOG)"`.
4. Restart: `docker compose restart`.
5. After the number of days you decided on, come back and fill in **"Result"**, and link the experiment in
   `EXPERIMENTS.md`.

A test fails if a config file's version has no matching entry (a line containing `<file> v<N>`).

---

## Template (copy this)

```
### YYYY-MM-DD: <file>.yaml v<N>: <one-line summary>
- **What changed:** (old value -> new value)
- **Why:** (the reason, in your own words, written before seeing results)
- **Decided by:** (name)
- **Expected effect:** (what you expect to happen, and how you will know)
- **Result after N days:** (fill in later; or "see EXPERIMENTS.md #<id>")
```

---

## Entries

### 2026-11-08: strategy.yaml v1: baseline example strategy
- **What changed:** New file. `example_momentum`: buy the ATM option when the NIFTY future moves 30+ points in 5 minutes;
  stop-loss 25% and target 40% of the premium; entries 09:30-14:30, square-off 15:15, at most 2 entries a day;
  nearest expiry but not on expiry day.
- **Why:** A deliberately simple template to learn the system. **Example only, not tested for profit.**
- **Decided by:** operator (defaults chosen at set-up).
- **Expected effect:** None claimed. It exists to produce paper trades to review.
- **Result after N days:** (fill in)

### 2026-11-08: risk.yaml v1: baseline safety limits
- **What changed:** New file. Max loss ₹2,000 per trade (at the stop) and ₹4,000 per day (realised + unrealised, then close
  everything); worst-case structure loss ₹10,000; 1 lot per order; 2 open positions; 5 order legs a day; entries
  09:20-15:00 Mon-Fri; forced square-off 15:20; **long options only**; spread rules and 3% / ₹2 maximum bid-ask spread.
- **Why:** Conservative starting limits chosen at set-up. NIFTY's lot is 65, so a 25% stop on a ₹100 option risks about ₹1,600.
- **Decided by:** operator.
- **Expected effect:** Limits losses; will refuse entries on options costing more than about ₹120 (roughly the last 2-3 days before expiry).
- **Result after N days:** (fill in)

### 2026-11-08: feed.yaml v1: market data settings
- **What changed:** New file. NIFTY, ATM ± 5 strikes, unhealthy after 10 s without a tick, reconnect after 30 s of silence, full quotes.
- **Why:** Enough strikes for the example strategy and the dashboard; bid/ask needed for the spread check.
- **Decided by:** operator.
- **Expected effect:** Stale prices block trading within 10 s.
- **Result after N days:** (fill in)

### 2026-11-08: execution.yaml v1: order and paper-fill settings
- **What changed:** New file. Orders priced 1% through the book (5% for emergency exits), cancelled after 20 s;
  paper fills at the touch ± 1 tick after 0.5 s; brokerage ₹20 per order.
- **Why:** Realistic-ish paper fills. Real fills will be worse.
- **Decided by:** operator.
- **Expected effect:** Paper results are an optimistic upper bound.
- **Result after N days:** (fill in)

### 2026-11-08: webhook.yaml v1: TradingView receiver
- **What changed:** New file. Listens on this computer only; NIFTY only; 20 requests/minute; lock out an address after 5 wrong secrets.
- **Why:** Safe defaults; exposure is only through the Cloudflare Tunnel path `/webhook`.
- **Decided by:** operator.
- **Expected effect:** None on trading; protects the endpoint.
- **Result after N days:** (fill in)

### 2026-11-08: journal.yaml v1: recording
- **What changed:** New file. Snapshots of the option chain every 5 s, kept 60 days.
- **Why:** Data for the backtest and for reviewing days later.
- **Decided by:** operator.
- **Expected effect:** A few MB per trading day in `data/market.db`.
- **Result after N days:** (fill in)

### 2026-11-08: alerts.yaml v1: Telegram
- **What changed:** New file. All routine alerts on; feed trouble after 15 s; daily summary 15:45; commands accepted from the owner's private chat only.
- **Why:** Know what the system is doing without watching it.
- **Decided by:** operator.
- **Expected effect:** Roughly one message per signal, order and fill.
- **Result after N days:** (fill in)

### 2026-11-08: ops.yaml v1: daily operation
- **What changed:** New file. Daily restart 08:30 (postponed while any position is open); heartbeat every 60 s.
- **Why:** Start each day with today's contracts and a fresh broker login.
- **Decided by:** operator.
- **Expected effect:** A brief restart each morning.
- **Result after N days:** (fill in)
