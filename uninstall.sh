#!/usr/bin/env bash
# Remove the Local LLM Manager systemd service installed by install.sh.
# Your data (data/manager.db), settings (.env) and the virtualenv stay on disk;
# add --purge to delete them too. Models started by the manager keep running
# either way (the service only manages the dashboard, like Docker containers).
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ -n "${SUDO_USER:-}" ]; then
  echo "Run this as your normal user, not with sudo." >&2
  exit 1
fi

for unit in local-llm-manager dgxspark-manager; do
  if [ -f "/etc/systemd/system/${unit}.service" ]; then
    sudo systemctl disable --now "$unit" || true
    sudo rm -f "/etc/systemd/system/${unit}.service"
    echo "Removed the ${unit} service."
  fi
done
sudo systemctl daemon-reload

if [ "$PURGE" = 1 ]; then
  rm -rf "${APP_DIR}/.venv" "${APP_DIR}/data" "${APP_DIR}/.env"
  # Settings from earlier versions, if they were never migrated away.
  rm -f "${HOME}/.config/local-llm-manager.env" "${HOME}/.config/dgxspark-manager.env"
  echo "Deleted .venv, data/ and .env."
else
  echo "Kept ${APP_DIR}/data, ${APP_DIR}/.env and .venv (rerun with --purge to delete them,"
  echo "or ./install.sh to install again)."
fi
