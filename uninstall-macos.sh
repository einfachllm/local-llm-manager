#!/usr/bin/env bash
# Remove the Local LLM Manager launchd agent installed by install-macos.sh.
# Your data (data/manager.db), settings (.env) and the virtualenv stay on disk;
# add --purge to delete them too. Model processes keep running either way
# (launchd only supervises the manager, like the containers do on Linux).
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
LABEL="com.local-llm-manager"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
OLD_PLIST="${HOME}/Library/LaunchAgents/com.dgxspark-manager.plist"
LOG="${HOME}/Library/Logs/local-llm-manager.log"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

# bootout takes the label when the plist is already gone, so try both.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
  || launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST" "$OLD_PLIST"
echo "Removed the ${LABEL} launchd agent."

if [ "$PURGE" = 1 ]; then
  rm -rf "${APP_DIR}/.venv" "${APP_DIR}/data" "${APP_DIR}/.env"
  rm -f "$LOG"
  echo "Deleted .venv, data/, .env and the service log."
else
  echo "Kept ${APP_DIR}/data, ${APP_DIR}/.env, .venv and ${LOG}"
  echo "(rerun with --purge to delete them, or ./install-macos.sh to install again)."
fi
