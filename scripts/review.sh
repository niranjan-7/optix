#!/usr/bin/env bash
# The evening review: for each trade, did it follow the rules? (interactive)
cd "$(dirname "$0")/.."
docker compose exec trader python -m trader.journal review --by "${1:-operator}"
