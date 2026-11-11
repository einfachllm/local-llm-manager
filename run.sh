#!/usr/bin/env bash
# Run the manager in the foreground from a local virtualenv.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  # Python 3.10+ is required (the code uses 3.10 syntax). PYTHON overrides the interpreter.
  PYTHON="${PYTHON:-python3}"
  if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "Python 3.10+ is required, but ${PYTHON} is $("$PYTHON" --version 2>&1 || echo 'not usable')." >&2
    echo "Install a newer Python and rerun, e.g.: PYTHON=python3.12 ./run.sh" >&2
    exit 1
  fi
  "$PYTHON" -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec .venv/bin/python -m local_llm_manager.main
