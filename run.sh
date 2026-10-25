#!/usr/bin/env bash
# Run the manager in the foreground from a local virtualenv.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec .venv/bin/python -m dgxspark_manager.main
