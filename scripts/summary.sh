#!/usr/bin/env bash
# The weekly performance summary (last 7 days). Pass extra options, e.g.  scripts/summary.sh --days 30
cd "$(dirname "$0")/.."
docker compose exec -T trader python -m trader.journal summary "$@"
