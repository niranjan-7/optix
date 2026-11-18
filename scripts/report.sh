#!/usr/bin/env bash
# Monthly P&L report. Examples:
#   scripts/report.sh                      last 12 months, printed on screen
#   scripts/report.sh --months 3           last 3 months
#   scripts/report.sh --csv /app/data/monthly.csv   also write an Excel file (appears as data/monthly.csv)
cd "$(dirname "$0")/.."
docker compose exec -T trader python -m trader.journal report "$@"
