# Security policy

This software can be connected to a brokerage account, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Use GitHub's private
"Report a vulnerability" feature (the *Security* tab of the repository) so it can be fixed before it is public.

Useful reports include: ways to bypass the risk engine, the live-trading confirmation, the kill switch, the webhook
authentication or the dashboard's local-only protections; anything that could leak a credential into a log, file or
network request; and unsafe defaults in the Docker setup.

## Please never include

Real API keys, TOTP secrets, PINs, Telegram tokens, webhook secrets, client IDs, account numbers, or screenshots that
show them. Redact before sending. If you accidentally exposed a credential, **revoke or regenerate it immediately**.

## What is in scope

The code in this repository and its default configuration. Weaknesses in Angel One, Telegram, TradingView, Cloudflare
or Docker themselves should be reported to those vendors.

## Safe use

- Keep `.env` private; it is git-ignored for a reason.
- Leave `LIVE_TRADING=false` until you have worked through `docs/go-live-checklist.md`.
- Publish only the webhook path through a tunnel, never the dashboard.
- Revoke any token you think may have leaked (Telegram: `/revoke` with BotFather; Angel One: regenerate in the SmartAPI portal).
