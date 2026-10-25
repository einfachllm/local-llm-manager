#!/usr/bin/env bash
# Install DGX Spark Manager as a systemd service running as the current user.
# The user must be able to run docker without sudo (member of the "docker" group).
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
ENV_FILE="${HOME}/.config/dgxspark-manager.env"

if ! docker info >/dev/null 2>&1; then
  echo "Cannot talk to docker as ${USER}. Run: sudo usermod -aG docker ${USER}  (then log in again)" >&2
  exit 1
fi

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created ${ENV_FILE} - edit it to set DGXM_PASSWORD, then: sudo systemctl restart dgxspark-manager"
fi

sudo tee /etc/systemd/system/dgxspark-manager.service >/dev/null <<UNIT
[Unit]
Description=DGX Spark Manager (model recipes dashboard)
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
User=${USER}
WorkingDirectory=${APP_DIR}
Environment=HOME=${HOME}
Environment=DGXM_DB=${APP_DIR}/data/manager.db
EnvironmentFile=-${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python -m dgxspark_manager.main
Restart=on-failure
RestartSec=5
# Only stop the manager itself; native-process models keep running across restarts (like containers).
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now dgxspark-manager

PORT="$(grep -E '^DGXM_PORT=' "$ENV_FILE" | cut -d= -f2)"
if grep -qE '^DGXM_MODE=agent' "$ENV_FILE"; then
  echo "Installed as agent. Add it in the hub under Hosts with URL http://$(hostname -I | awk '{print $1}'):${PORT:-8090}"
  echo "and the DGXM_AGENT_TOKEN from ${ENV_FILE}."
else
  echo "Installed. Open http://$(hostname -I | awk '{print $1}'):${PORT:-8090}"
fi
echo "Service logs: journalctl -u dgxspark-manager -f"
