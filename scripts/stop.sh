#!/usr/bin/env bash
# Stop the system. Open positions are NOT closed by stopping: close them first (dashboard or /kill).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down
