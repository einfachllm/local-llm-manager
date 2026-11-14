#!/usr/bin/env bash
# Install Local LLM Manager on macOS (e.g. a Mac Studio) as a launchd agent of the current user.
# Models run as native processes (MLX, llama.cpp, ...); Docker is optional.
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
if [ -n "${SUDO_USER:-}" ]; then
  echo "Run this as your normal user, not with sudo: the service must run as you." >&2
  exit 1
fi
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

# Stop the running agent before touching its virtualenv. Upgrade from the old name
# (DGX Spark Manager): retire its launchd agent too.
OLD_PLIST="${HOME}/Library/LaunchAgents/com.dgxspark-manager.plist"
if [ -f "$OLD_PLIST" ]; then
  launchctl bootout "gui/$(id -u)" "$OLD_PLIST" 2>/dev/null || true
  rm -f "$OLD_PLIST"
fi
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
  || launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true

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

# launchd runs this wrapper instead of run.sh directly: on a configuration error (exit 78, e.g. a
# weak password) it leaves a marker file behind. The plist's KeepAlive PathState watches that marker,
# so launchd stops restarting instead of looping. Fix .env and rerun ./install-macos.sh (or delete
# the marker) to start again.
WRAPPER="${APP_DIR}/.venv/bin/llm-manager-wrap"
cat > "$WRAPPER" <<'WRAP'
#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/../.." || exit 1
MARKER="data/.config-fault"
mkdir -p data
rm -f "$MARKER"
./run.sh
code=$?
if [ "$code" -eq 78 ]; then
  touch "$MARKER"
fi
exit "$code"
WRAP
chmod +x "$WRAPPER"

setting() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }
PORT="$(setting DGXM_PORT)"
TS_CLI="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
# Where the health check below knocks: with DGXM_HOST=tailscale the manager does not listen on 127.0.0.1.
case "$(setting DGXM_HOST)" in
  ""|0.0.0.0) CHECK_HOST="127.0.0.1" ;;
  tailscale)  CHECK_HOST="$("$TS_CLI" ip -4 2>/dev/null | head -n 1 || true)" ;;
  *)          CHECK_HOST="$(setting DGXM_HOST)" ;;
esac

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key><array><string>${WRAPPER}</string></array>
  <key>WorkingDirectory</key><string>${APP_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- launchd starts with a minimal PATH; model servers (mlx_lm.server, llama-server) are found through this. -->
    <key>PATH</key><string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>${HOME}</string>
    <key>DGXM_DB</key><string>${APP_DIR}/data/manager.db</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- Restart on crashes, but not on configuration errors (exit 78): the wrapper leaves
       data/.config-fault behind, and PathState keeps the job stopped while that file exists. -->
  <key>KeepAlive</key>
  <dict>
    <key>PathState</key>
    <dict>
      <key>${APP_DIR}/data/.config-fault</key><false/>
    </dict>
  </dict>
  <!-- Model processes run in their own process groups and must survive a manager restart. -->
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$PLIST"

# Did it come up? Configuration errors are reported right at start-up (and stop the restart loop).
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf "http://${CHECK_HOST:-127.0.0.1}:${PORT:-8090}/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  echo "The service did not start. Its last log lines:" >&2
  tail -n 8 "$LOG" >&2 || true
  echo "Fix ${APP_DIR}/.env, then rerun ./install-macos.sh" >&2
  exit 1
fi

case "$(setting DGXM_HOST)" in
  ""|0.0.0.0) IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)" ;;
  tailscale)  IP="$("$TS_CLI" ip -4 2>/dev/null | head -n 1 || true)"; IP="${IP:-$(hostname -s)}" ;;
  *)          IP="$(setting DGXM_HOST)" ;;
esac
if [ "$(setting DGXM_MODE)" = "agent" ] && [ -n "$(setting DGXM_HUB_URL)" ]; then
  CODE="$(.venv/bin/python -m local_llm_manager.joining code "$(setting DGXM_AGENT_TOKEN)")"
  echo "Installed as agent. It asks the hub $(setting DGXM_HUB_URL) to add it."
  echo "On the hub, open Hosts and approve it only if it shows this code:  ${CODE}"
elif [ "$(setting DGXM_MODE)" = "agent" ]; then
  echo "Installed as agent. In the hub dashboard, Hosts -> Add host:"
  echo "  URL:   http://${IP}:${PORT:-8090}"
  echo "  Token: the DGXM_AGENT_TOKEN from ${APP_DIR}/.env"
else
  echo "Installed. Open http://${IP}:${PORT:-8090}"
fi
echo "Service log: tail -f ${LOG}"
echo "Uninstall:   ./uninstall-macos.sh"