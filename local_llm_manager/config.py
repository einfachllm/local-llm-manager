"""Runtime configuration, read from environment variables."""

import os
from pathlib import Path

DB_PATH = Path(os.environ.get("DGXM_DB", Path(__file__).resolve().parent.parent / "data" / "manager.db"))
# State and log files of native-process recipes.
PROC_DIR = Path(os.environ.get("DGXM_PROC_DIR", DB_PATH.parent / "processes"))
# Listen address of the manager. "tailscale" = this machine's Tailscale IPv4 (resolved at startup), so the
# dashboard / agent API is only reachable through the tailnet.
HOST = os.environ.get("DGXM_HOST", "0.0.0.0")
PORT = int(os.environ.get("DGXM_PORT", "8090"))

# "hub" (default): dashboard, database and scheduler. "agent": only the token-protected agent API,
# so a hub on another machine can run models here.
MODE = os.environ.get("DGXM_MODE", "hub").strip().lower()
AGENT_TOKEN = os.environ.get("DGXM_AGENT_TOKEN", "")

# Optional HTTP Basic auth. Strongly recommended when the port is reachable from the network,
# since this service can run arbitrary docker containers.
AUTH_USER = os.environ.get("DGXM_USER", "admin")
AUTH_PASSWORD = os.environ.get("DGXM_PASSWORD", "")

# Address model ports are published on: `docker run -p <ip>:<port>:...` and {bind_host} in commands of
# native / host-network recipes. Empty = all interfaces; "tailscale" = this machine's Tailscale IPv4.
PUBLISH_IP = os.environ.get("DGXM_PUBLISH_IP", "").strip()

# Host used to probe model health endpoints (the published host port is appended).
# Default: the publish address if one is set (models do not listen on 127.0.0.1 then), else 127.0.0.1.
HEALTH_HOST = os.environ.get("DGXM_HEALTH_HOST", "") or (
    PUBLISH_IP if PUBLISH_IP and PUBLISH_IP != "tailscale" else "127.0.0.1")

# Directories scanned for local model files (e.g. GGUF, safetensors) when checking whether a
# recipe's model is present on this host. Comma- or whitespace-separated.
MODEL_DIRS = [d.strip() for d in os.environ.get("DGXM_MODEL_DIR", "~/models").replace(",", " ").split() if d.strip()]

# Docker binary and timeouts.
DOCKER_BIN = os.environ.get("DGXM_DOCKER", "docker")
STOP_TIMEOUT = int(os.environ.get("DGXM_STOP_TIMEOUT", "30"))

# Image used to drop the page cache when the manager itself lacks permission
# (runs `sync; echo 3 > /proc/sys/vm/drop_caches` in a privileged throwaway container).
DROP_CACHES_IMAGE = os.environ.get("DGXM_DROP_CACHES_IMAGE", "busybox:latest")

# Scheduler tick and grace period for one-shot schedules missed while the manager was down.
SCHEDULER_TICK = float(os.environ.get("DGXM_SCHEDULER_TICK", "10"))
ONCE_GRACE_SECONDS = int(os.environ.get("DGXM_ONCE_GRACE_SECONDS", "600"))

# Days to keep activity-log entries and start-time measurements (0 = keep them forever).
# Pruned on startup, so the database cannot grow without bound.
RETENTION_DAYS = int(os.environ.get("DGXM_RETENTION_DAYS", "90"))

# Serve the manager's own metrics (Prometheus format) at /metrics. /healthz is always available.
METRICS_ENABLED = os.environ.get("DGXM_METRICS", "").strip().lower() in ("1", "true", "yes")


TAILSCALE_CLI = ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def tailscale_ip() -> str:
    """This machine's Tailscale IPv4 (`tailscale ip -4`); SystemExit with a hint if unavailable."""
    import shutil
    import subprocess

    for cli in TAILSCALE_CLI:
        path = shutil.which(cli) or (cli if os.path.isfile(cli) else None)
        if not path:
            continue
        try:
            out = subprocess.run([path, "ip", "-4"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
        if ip:
            return ip
    raise SystemExit("DGXM_HOST / DGXM_PUBLISH_IP is 'tailscale', but no Tailscale IPv4 was found. "
                     "Is Tailscale installed and connected (`tailscale ip -4`)?")


def resolve_network() -> None:
    """Replace the value "tailscale" in DGXM_HOST / DGXM_PUBLISH_IP with this machine's Tailscale IP."""
    global HOST, PUBLISH_IP, HEALTH_HOST
    if "tailscale" in (HOST, PUBLISH_IP):
        ip = tailscale_ip()
        HOST = ip if HOST == "tailscale" else HOST
        if PUBLISH_IP == "tailscale":
            PUBLISH_IP = ip
            if not os.environ.get("DGXM_HEALTH_HOST"):
                HEALTH_HOST = ip
