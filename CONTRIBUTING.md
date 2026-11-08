# Contributing

Thanks for wanting to help. This project exists to be a **safe, readable, open way to learn algorithmic options
trading with Angel One's SmartAPI**: paper trading first, with a strict risk gate and a full audit trail. Because
people may connect it to real money, a few rules matter more than any feature.

## The safety rules (a pull request must never break these)

1. **Paper by default.** Real orders happen only if `LIVE_TRADING=true` **and** a person types the confirmation phrase
   at start-up. Never add a way around this, a default that enables it, or an "auto-confirm" option.
2. **One gate.** Every signal (strategy, webhook, anything new) goes through `RiskEngine.evaluate()`. Nothing reaches a
   broker adapter without the `ApprovedOrder` it returns. New order paths must call `require_approved`.
3. **No secrets** in code, logs, tests, screenshots, issues or pull requests. Use `.env` and `.env.example` (empty values only).
4. **No naked option selling.** Only defined-risk structures. The structure rules in `structures.py` are a whitelist; do not loosen them.
5. **No profitability claims.** Do not describe any strategy, rule set or backtest as profitable or as "working" in docs,
   examples or comments. Examples say "example only, not tested for profit".
6. **Tests.** Changes to risk, execution, brokers, structures, greeks, webhook security, kill switch or the live gate
   need tests, including tests that would **fail** if the safeguard were removed.

If a change touches rules 1 to 4, please say so in the pull request description and explain how it stays safe.

## Getting set up

```bash
git clone <your fork>
cd <repo>
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pip install -e .
python -m pytest -q
```

All tests should pass without any credentials and without internet access (except the optional Docker check, which skips if Docker is missing).

## Making a change

- Keep it **simple and readable**. Comment the *why*, not the *what*.
- Add or update tests. Run `python -m pytest -q`.
- Changing a file in `config/`? Raise its `version:` and add a `config/CHANGELOG.md` entry (a test checks this).
- Update the docs if behaviour changes (README, `docs/`).
- Keep pull requests small and focused: one idea per pull request.

## Good first contributions

- Windows PowerShell equivalents of the `scripts/*.sh` helpers.
- More example strategies (clearly marked "example only"), each with tests and a YAML rule file.
- Support for another underlying (for example BANKNIFTY): config, lot sizes and tests.
- Recorded-fixture tests for the Angel One adapters (never include real credentials or account data).
- Documentation fixes, translations, and screenshots of the dashboard.
- Accessibility and mobile improvements to the dashboard.

## Things that are hard to test without a broker account

The live Angel One feed and order adapter have never been run against the real servers by the maintainers. If you
have a SmartAPI account and can verify them **in paper or minimal-size conditions**, a report of what matched and what
did not is very valuable. Please redact all account details.

## Conduct

Be kind and assume good faith. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).
