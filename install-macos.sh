#!/usr/bin/env bash
# Install Local LLM Manager on macOS (e.g. a Mac Studio) as a launchd agent of the current user.
# Models run as native processes (MLX, llama.cpp, ...); Docker is optional.
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
LABEL="com.local-llm-manager"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG="${HOME}/Library/Logs/local-llm-manager.log"

# Python 3.10+ is required (the Xcode python3 is 3.9). Homebrew: brew install python@3.12
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for p in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      PYTHON="$(command -v "$p")"; break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "Python 3.10+ not found. Install it with: brew install python@3.12" >&2
  exit 1
fi
echo "Using $PYTHON"

# The script import needs bash 4+ to record skipped commands (macOS ships bash 3.2).
if ! command -v /opt/homebrew/bin/bash >/dev/null 2>&1 && ! command -v /usr/local/bin/bash >/dev/null 2>&1; then
  echo "Note: for 'Import script…' install a current bash: brew install bash"
fi

rm -rf .venv
"$PYTHON" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created ${APP_DIR}/.env"
fi
# Replaces the example password change-me with a random one (printed), fills an empty agent token, chmod 600.
.venv/bin/python -m local_llm_manager.envfile .env

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key><array><string>${APP_DIR}/run.sh</string></array>
  <key>WorkingDirectory</key><string>${APP_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- launchd starts with a minimal PATH; model servers (mlx_lm.server, llama-server) are found through this. -->
    <key>PATH</key><string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>${HOME}</string>
    <key>DGXM_DB</key><string>${APP_DIR}/data/manager.db</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- Model processes run in their own process groups and must survive a manager restart. -->
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
</dict>
</plist>
PLIST

# Upgrade from the old name (DGX Spark Manager): retire its launchd agent.
OLD_PLIST="${HOME}/Library/LaunchAgents/com.dgxspark-manager.plist"
if [ -f "$OLD_PLIST" ]; then
  launchctl bootout "gui/$(id -u)" "$OLD_PLIST" 2>/dev/null || true
  rm -f "$OLD_PLIST"
fi
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

setting() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }
PORT="$(setting DGXM_PORT)"
TS_CLI="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
case "$(setting DGXM_HOST)" in
  ""|0.0.0.0) IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)" ;;
  tailscale)  IP="$("$TS_CLI" ip -4 2>/dev/null | head -n 1 || true)"; IP="${IP:-$(hostname -s)}" ;;
  *)          IP="$(setting DGXM_HOST)" ;;
esac
if [ "$(setting DGXM_MODE)" = "agent" ]; then
  echo "Installed as agent. In the hub dashboard, Hosts -> Add host:"
  echo "  URL:   http://${IP}:${PORT:-8090}"
  echo "  Token: the DGXM_AGENT_TOKEN from ${APP_DIR}/.env"
else
  echo "Installed. Open http://${IP}:${PORT:-8090}"
fi
echo "Service log: tail -f ${LOG}"
echo "Uninstall:   ./uninstall-macos.sh"
