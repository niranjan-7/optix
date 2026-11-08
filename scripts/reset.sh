#!/usr/bin/env bash
# Re-enable trading after a kill. Asks you to type RESET. Usage: scripts/reset.sh "Your Name" "why it is safe"
cd "$(dirname "$0")/.."
[ $# -ge 2 ] || { echo 'Usage: scripts/reset.sh "Your Name" "why it is safe"'; exit 2; }
docker compose exec trader python -m trader.risk reset --operator "$1" --note "$2"
