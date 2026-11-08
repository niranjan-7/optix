#!/usr/bin/env bash
# Follow the program's log. Ctrl-C stops watching (the program keeps running).
cd "$(dirname "$0")/.."
docker compose logs -f --tail 100 trader
