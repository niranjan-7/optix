#!/usr/bin/env bash
# EMERGENCY: stop all new orders now; the running program then cancels orders and closes positions.
cd "$(dirname "$0")/.."
docker compose exec -T trader python -m trader.risk kill --reason "${1:-manual kill from the command line}"
