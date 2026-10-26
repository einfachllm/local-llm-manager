#!/usr/bin/env bash
# Install Local LLM Manager as a systemd service running as the current user.
# The user must be able to run docker without sudo (member of the "docker" group).
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
# Settings live in .env next to the code - the same file run.sh, docker compose and install-macos.sh use.
ENV_FILE="${APP_DIR}/.env"
LEGACY_ENV_FILE="${HOME}/.config/local-llm-manager.env"

if ! docker info >/dev/null 2>&1; then
  echo "Cannot talk to docker as ${USER}. Run: sudo usermod -aG docker ${USER}  (then log in again)" >&2
  exit 1
fi

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Earlier versions kept the settings in ~/.config (and before that under the old name): move them here
# if there is no .env yet. If both exist, .env wins and the old file is no longer read.
for old in "$LEGACY_ENV_FILE" "${HOME}/.config/dgxspark-manager.env"; do
  if [ -f "$old" ] && [ ! -f "$ENV_FILE" ]; then
    mv "$old" "$ENV_FILE"
    echo "Moved your settings from ${old} to ${ENV_FILE}"
  elif [ -f "$old" ]; then
    echo "Note: ${old} is no longer read - your settings are in ${ENV_FILE} (you can delete the old file)."
  fi
done
if [ -f /etc/systemd/system/dgxspark-manager.service ]; then
  sudo systemctl disable --now dgxspark-manager || true
  sudo rm -f /etc/systemd/system/dgxspark-manager.service
fi
if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  echo "Created ${ENV_FILE}"
fi
# Replaces the example password change-me with a random one (printed), fills an empty agent token, chmod 600.
.venv/bin/python -m local_llm_manager.envfile "$ENV_FILE"

sudo tee /etc/systemd/system/local-llm-manager.service >/dev/null <<UNIT
[Unit]
Description=Local LLM Manager (model recipes dashboard)
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
User=${USER}
WorkingDirectory=${APP_DIR}
Environment=HOME=${HOME}
Environment=DGXM_DB=${APP_DIR}/data/manager.db
EnvironmentFile=-${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python -m local_llm_manager.main
Restart=on-failure
RestartSec=5
# 78 = configuration error (e.g. weak password): retrying cannot help, so don't loop; fix .env and restart.
RestartPreventExitStatus=78
# Only stop the manager itself; native-process models keep running across restarts (like containers).
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable local-llm-manager
# restart, not start: rerunning the script after `git pull` must load the new code.
sudo systemctl restart local-llm-manager

# Did it come up? Configuration errors are reported right at start-up.
sleep 3
if ! systemctl is-active --quiet local-llm-manager; then
  echo "The service did not start. Its last log lines:" >&2
  journalctl -u local-llm-manager -n 8 --no-pager -o cat >&2 || true
  echo "Fix ${ENV_FILE}, then: sudo systemctl restart local-llm-manager" >&2
  exit 1
fi

setting() { grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true; }
PORT="$(setting DGXM_PORT)"
case "$(setting DGXM_HOST)" in
  ""|0.0.0.0) ADDR="$(hostname -I | awk '{print $1}')" ;;
  tailscale)  ADDR="$(hostname -s) (Tailscale: $(tailscale ip -4 2>/dev/null | head -n 1))" ;;
  *)          ADDR="$(setting DGXM_HOST)" ;;
esac
URL="http://${ADDR%% *}:${PORT:-8090}"
if [ "$(setting DGXM_MODE)" = "agent" ]; then
  echo "Installed as agent on ${ADDR}. In the hub: Hosts -> Add host with URL ${URL}"
  echo "and the DGXM_AGENT_TOKEN from ${ENV_FILE}."
else
  echo "Installed and running on ${ADDR}. Open ${URL} (user: $(setting DGXM_USER))"
fi
echo "Settings: ${ENV_FILE}   Logs: journalctl -u local-llm-manager -f"
