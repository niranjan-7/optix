#!/usr/bin/env bash
# Start (or rebuild and start) the system in the background.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env file. Copy .env.example to .env and fill it in first (see README)."; exit 1; }
docker compose up -d --build
echo "Started. Dashboard: http://127.0.0.1:8000/   Logs: scripts/logs.sh"
