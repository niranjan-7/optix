## What and why

<!-- One or two sentences. Link an issue if there is one. -->

## Checklist

- [ ] `python -m pytest -q` passes
- [ ] I added or updated tests (for safety-related code: tests that would **fail** if the safeguard were removed)
- [ ] No secrets, tokens, account numbers or personal data anywhere in this change
- [ ] Docs updated if behaviour changed
- [ ] If I changed `config/`, I raised the `version:` and added a `config/CHANGELOG.md` entry

## Safety rules (see CONTRIBUTING.md)

- [ ] Paper stays the default; live trading still needs `LIVE_TRADING=true` **and** the typed confirmation
- [ ] Every order path still goes through the risk engine (`require_approved`)
- [ ] No naked option selling; structure rules not loosened
- [ ] No claims that a strategy is profitable

**Does this touch any of the above?** <!-- If yes, explain how it stays safe. -->
