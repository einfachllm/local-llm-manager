"""Runtime configuration, read from environment variables."""

import os
from pathlib import Path

DB_PATH = Path(os.environ.get("DGXM_DB", Path(__file__).resolve().parent.parent / "data" / "manager.db"))
# State and log files of native-process recipes.
PROC_DIR = Path(os.environ.get("DGXM_PROC_DIR", DB_PATH.parent / "processes"))
HOST = os.environ.get("DGXM_HOST", "0.0.0.0")
PORT = int(os.environ.get("DGXM_PORT", "8090"))

# Optional HTTP Basic auth. Strongly recommended when the port is reachable from the network,
# since this service can run arbitrary docker containers.
AUTH_USER = os.environ.get("DGXM_USER", "admin")
AUTH_PASSWORD = os.environ.get("DGXM_PASSWORD", "")

# Host used to probe model health endpoints (the published host port is appended).
HEALTH_HOST = os.environ.get("DGXM_HEALTH_HOST", "127.0.0.1")

# Docker binary and timeouts.
DOCKER_BIN = os.environ.get("DGXM_DOCKER", "docker")
STOP_TIMEOUT = int(os.environ.get("DGXM_STOP_TIMEOUT", "30"))

# Image used to drop the page cache when the manager itself lacks permission
# (runs `sync; echo 3 > /proc/sys/vm/drop_caches` in a privileged throwaway container).
DROP_CACHES_IMAGE = os.environ.get("DGXM_DROP_CACHES_IMAGE", "busybox:latest")

# Scheduler tick and grace period for one-shot schedules missed while the manager was down.
SCHEDULER_TICK = float(os.environ.get("DGXM_SCHEDULER_TICK", "10"))
ONCE_GRACE_SECONDS = int(os.environ.get("DGXM_ONCE_GRACE_SECONDS", "600"))
