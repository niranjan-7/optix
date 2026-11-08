#!/usr/bin/env bash
# Is it running, and what does the kill switch say?
cd "$(dirname "$0")/.."
docker compose ps
docker compose exec -T trader python -m trader.risk status
