"""Where recipes actually run: on this machine (LocalExecutor) or on an agent (RemoteExecutor).

The hub keeps recipes, schedules and the activity log; executors only run what they are given.
Both executors have the same async interface, so the Manager does not care where a host is.
"""

import asyncio
import logging
import os
import platform
import re
import socket
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from . import __version__, config, logstats, servermetrics, system
from .docker import Docker, try_drop_caches_locally
from .processes import ProcessError, ProcessRunner
from .recipes import (build_process_argv, build_run_argv, format_command, format_process_command, process_env,
                      secret_names)

# Matches a Hugging Face repo id: exactly one "/" with no path-like characters on either side.
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _hf_cache_dir() -> Path:
    root = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    return Path(root) / "hub"


def _volume_host_paths(volumes: str) -> dict[str, str]:
    """container path -> host path, parsed from a recipe's volumes field (one `host:container[:mode]` per line)."""
    out: dict[str, str] = {}
    for line in (volumes or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split on colons, keeping the container path as the second field.
        parts = line.split(":")
        if len(parts) >= 2:
            host, cont = parts[0].strip(), parts[1].strip()
            if host and cont:
                out[cont] = host
    return out


_MODEL_FILE_EXTS = (".gguf", ".safetensors", ".pt", ".bin", ".onnx", ".mlx", ".npz", ".tar")


def _resolve_model_path(model: str, volumes: str) -> Optional[str]:
    """If `model` is a container path covered by a volume mount, return the corresponding raw host path."""
    value = model.strip()
    if not value.startswith("/"):
        return None
    mounts = _volume_host_paths(volumes)
    # Exact match first, then the closest parent mount (e.g. model=/models/sub maps to a /models mount).
    if value in mounts:
        return mounts[value]
    for cont, host in mounts.items():
        if value.startswith(cont.rstrip("/") + "/"):
            return host.rstrip("/") + value[len(cont.rstrip("/")):]
    return None


def resolve_model_target(model: str, volumes: str) -> dict[str, str]:
    """Resolve a recipe's model value to a host-side target, without touching the filesystem.

    kind "volume": the model is a container path backed by a volume mount (reference = the host
    path from the mount); kind "hf": a Hugging Face repo id, checked against the local hub cache;
    kind "local": a file path, resolved against the host's model dirs.
    """
    value = model.strip()
    # Normalise HF URLs to bare repo ids.
    for prefix in ("https://huggingface.co/", "hf.co/", "huggingface.co/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if host_path := _resolve_model_path(value, volumes):
        return {"kind": "volume", "reference": host_path, "host_path": host_path}
    if _HF_REPO_RE.match(value) and not value.lower().endswith(_MODEL_FILE_EXTS):
        return {"kind": "hf", "reference": value, "repo": value}
    return {"kind": "local", "reference": value, "path": value}


def check_model_target(target: dict[str, str], model_dirs: list[str]) -> dict[str, Any]:
    """Host-side existence check for a resolved model target (see resolve_model_target)."""
    kind = target["kind"]
    if kind == "hf":
        expected = str(_hf_cache_dir() / ("models--" + target["repo"].replace("/", "--")))
        found = expected if os.path.isdir(expected) else None
    elif kind == "volume":
        expected = os.path.expanduser(target["host_path"])
        found = expected if os.path.exists(expected) else None
    else:  # local: the raw path, or that file name inside one of the configured model dirs
        candidates = [os.path.expanduser(target["path"])]
        for d in model_dirs:
            candidates.append(os.path.join(os.path.expanduser(d), target["path"]))
        found = next((c for c in candidates if os.path.exists(c)), None)
        expected = candidates[0]
    return {
        "reference": target["reference"],
        "present": found is not None,
        "location": found,
        "expected": None if found else expected,
        "size_gb": _size_gb(found) if found else None,
    }


def _contract_home(path: str) -> str:
    """/home/me/models/x -> ~/models/x: the same layout on another machine (other user name) matches."""
    home = os.path.expanduser("~").rstrip("/")
    return "~" + path[len(home):] if home and (path == home or path.startswith(home + "/")) else path


def _size_gb(path: str) -> Optional[float]:
    """Total size in GB of a file or directory (summed over its contents), or None when unreadable."""
    try:
        if os.path.isdir(path):
            total = 0
            for dirpath, _, files in os.walk(path):
                for f in files:
                    try:
                        total += os.stat(os.path.join(dirpath, f)).st_size
                    except OSError:
                        pass
            return round(total / 1e9, 1)
        return round(os.path.getsize(path) / 1e9, 1)
    except OSError:
        return None


_WEIGHT_FILE_EXTS = (".safetensors", ".gguf", ".bin", ".pt", ".npz")
SCAN_MAX_DEPTH = 3
SCAN_MAX_RESULTS = 500


def scan_weight_dirs(dirs: list[str]) -> list[dict[str, Any]]:
    """Model folders (with config.json or weight files) and loose .gguf files under the given directories,
    up to SCAN_MAX_DEPTH levels deep. Paths under the home directory are returned as ~/..., so the same
    layout matches on machines with other user names. The Hugging Face cache is not listed: its snapshot
    folders hold symlinks into the cache that a single mount cannot follow."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in dirs:
        root = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
            weights = [f for f in filenames if f.lower().endswith(_WEIGHT_FILE_EXTS)]
            if dirpath != root and ("config.json" in filenames or any(not f.endswith(".gguf") for f in weights)):
                size = 0
                for sub, _, files in os.walk(dirpath):
                    for f in files:
                        try:
                            size += os.stat(os.path.join(sub, f)).st_size
                        except OSError:
                            pass
                if dirpath not in seen:
                    seen.add(dirpath)
                    found.append({"path": _contract_home(dirpath), "name": os.path.basename(dirpath),
                                  "kind": "dir", "size_gb": round(size / 1e9, 1)})
                dirnames[:] = []  # a model folder: don't list its sub-folders as models too
                continue
            for f in sorted(filenames):
                if f.lower().endswith(".gguf"):
                    full = os.path.join(dirpath, f)
                    try:
                        size = os.stat(full).st_size
                    except OSError:
                        size = 0
                    found.append({"path": _contract_home(full), "name": f, "kind": "file", "size_gb": round(size / 1e9, 1)})
            if dirpath.rstrip("/").count("/") - base_depth >= SCAN_MAX_DEPTH:
                dirnames[:] = []
            if len(found) >= SCAN_MAX_RESULTS:
                return found[:SCAN_MAX_RESULTS]
    return found


class ActionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


RUNNING_STATES = ("ready", "loading", "starting")

# Log tail read per running recipe to pick up the latest engine stats line (vLLM prints one ~every 10s).
METRICS_TAIL = 80


def _instance_key(recipe: dict, instance: dict) -> str:
    """Counters restart with the container/process, so rates are tracked per instance."""
    return f"{recipe['id']}:{instance.get('id', '')}"


def host_info() -> dict[str, Any]:
    return {"hostname": socket.gethostname(), "platform": sys.platform, "machine": platform.machine(),
            "version": __version__}


class LocalExecutor:
    def __init__(self, docker: Optional[Docker] = None, procs: Optional[ProcessRunner] = None):
        self.docker = docker or Docker()
        self.procs = procs or ProcessRunner(config.PROC_DIR)
        self.rates = servermetrics.RateTracker()

    def _stop_marker(self, name: str) -> Path:
        """File that records the container id the manager last stopped (`<name>.stopped`).

        Docker cannot be asked who stopped a container, and `docker stop` and an OOM kill both
        end in exit code 137 - the marker (surviving manager restarts, like the containers) is
        what tells the two apart.
        """
        return self.procs.dir / f"{name}.stopped"

    def _write_stop_marker(self, name: str, container_id: str) -> None:
        if container_id:
            self._stop_marker(name).write_text(container_id)

    def _clear_stop_marker(self, name: str) -> None:
        self._stop_marker(name).unlink(missing_ok=True)

    def _was_stopped_by_manager(self, name: str, container_id: str) -> bool:
        marker = self._stop_marker(name)
        try:
            return bool(container_id) and marker.read_text().strip() == container_id
        except OSError:
            return False

    async def info(self) -> dict[str, Any]:
        return host_info()

    async def system(self) -> dict[str, Any]:
        return await system.snapshot()

    async def _instances(self, recipes: list[dict]) -> dict[int, Optional[dict]]:
        """Container (docker) or process (native) info per recipe id, in the same shape."""
        need_docker = any(r.get("runtime", "docker") == "docker" for r in recipes)
        containers = await self.docker.list_containers() if need_docker else {}
        return {
            r["id"]: self.procs.status(r["container_name"]) if r.get("runtime") == "process"
            else containers.get(r["container_name"])
            for r in recipes
        }

    async def _healthy(self, client: httpx.AsyncClient, recipe: dict) -> bool:
        path = recipe.get("health_path") or ""
        if not path:
            return True
        try:
            r = await client.get(f"http://{config.HEALTH_HOST}:{recipe['host_port']}{path}")
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    async def statuses(self, recipes: list[dict]) -> dict[int, dict[str, Any]]:
        """{recipe id: {"status": ready|loading|starting|stopped|failed, "container": {...} | None}}."""
        instances = await self._instances(recipes)
        out: dict[int, dict[str, Any]] = {}
        running = []
        for r in recipes:
            c = instances[r["id"]]
            if c is None:
                status = "stopped"
            elif c["state"] == "running":
                status = "running"
                running.append(r)
            elif c["state"] in ("created", "restarting"):
                status = "starting"
            elif r.get("runtime") == "process":
                status = "failed" if c.get("failed") else "stopped"
            else:
                stopped_by_us = self._was_stopped_by_manager(r["container_name"], c["id"])
                status = "failed" if _failed_exit(c["status"], stopped_by_us) else "stopped"
            out[r["id"]] = {"status": status, "container": c}
            if r.get("kind") == "agent":
                out[r["id"]].update(await self._agent_checks(r))
        if running:
            # Agents serve no LLM API, so there are no engine metrics to read.
            with_metrics = [r for r in running if r.get("kind") != "agent"]
            async with httpx.AsyncClient(timeout=2.0) as client:
                health = await asyncio.gather(*(self._healthy(client, r) for r in running))
                stats = await asyncio.gather(*(self._metrics(client, r, instances[r["id"]]) for r in with_metrics))
            for r, ok in zip(running, health):
                out[r["id"]]["status"] = "ready" if ok else "loading"
            for r, m in zip(with_metrics, stats):
                if m:
                    out[r["id"]]["metrics"] = m
        self.rates.forget_except({_instance_key(r, instances[r["id"]]) for r in running})
        return out

    async def _agent_checks(self, recipe: dict) -> dict[str, Any]:
        """For agents: which passed-through secrets are set here (names only, never values), and
        other copies of a native agent's command running outside the manager (e.g. a detached
        gateway that would poll the same bot token)."""
        out: dict[str, Any] = {"secrets": {k: bool(os.environ.get(k)) for k in secret_names(recipe)}}
        if recipe.get("runtime") == "process":
            try:
                argv = build_process_argv(recipe)
            except ValueError:
                argv = []
            out["duplicates"] = await asyncio.to_thread(self.procs.duplicates, recipe["container_name"], argv)
        return out

    async def _metrics(self, client: httpx.AsyncClient, recipe: dict, instance: dict) -> dict:
        """Live numbers from the server's /metrics, else the latest stats line in its logs; {} if neither."""
        try:
            r = await client.get(f"http://{config.HEALTH_HOST}:{recipe['host_port']}/metrics")
            if r.status_code == 200:
                m = self.rates.update(_instance_key(recipe, instance), r.text)
                if m:
                    return {**m, "source": "metrics"}
        except httpx.HTTPError:
            pass
        try:
            m = logstats.from_logs(await self.logs(recipe, tail=METRICS_TAIL))
        except ActionError:
            return {}
        return {**m, "source": "log"} if m else {}

    async def start(self, recipe: dict) -> str:
        """Start a recipe that is not running. Returns a message (first line) + the command used."""
        name = recipe["container_name"]
        # The Manager already refused ports of running recipes; anything still listening is foreign.
        if await asyncio.to_thread(port_in_use, recipe["host_port"]):
            raise ActionError(f"Port {recipe['host_port']} on {socket.gethostname()} is already in use by a program "
                              "the manager did not start. Stop that program or choose another port.", 409)
        if recipe.get("runtime") == "process":
            try:
                pid = self.procs.start(name, build_process_argv(recipe), process_env(recipe))
            except (ProcessError, OSError) as e:
                raise ActionError(f"Could not start process: {e}", 500)
            return f"Started {recipe['name']} (pid {pid})\n{format_process_command(recipe)}"
        existing = (await self._instances([recipe]))[recipe["id"]]
        if existing:
            await self.docker.remove(name)
        # A fresh run clears the "manager stopped it" marker, so a later OOM kill is not read
        # as a clean stop just because this recipe was stopped before.
        self._clear_stop_marker(name)
        argv = build_run_argv(recipe)
        res = await self.docker.run_argv(argv)
        if not res.ok:
            raise ActionError(f"docker run failed: {res.error}", 500)
        return f"Started {recipe['name']} ({res.stdout.strip()[:12]})\n{format_command(argv)}"

    async def stop(self, recipe: dict) -> str:
        name = recipe["container_name"]
        if recipe.get("runtime") == "process":
            stopped = await self.procs.stop(name)
            return f"Stopped {recipe['name']}" if stopped else f"{recipe['name']} is not running"
        cid = (await self._instances([recipe]))[recipe["id"]]
        cid = cid["id"] if cid else ""
        res = await self.docker.stop(name)
        if not res.ok:
            raise ActionError(f"docker stop failed: {res.error}", 500)
        # Remember (id + file) that we stopped it: its usual 137/143 exit code is then read as a
        # clean stop, while an OOM-killed container (same codes, no marker) shows as failed.
        try:
            self._write_stop_marker(name, cid)
        except OSError as e:
            logging.warning("Could not write stop marker for %s: %s", name, e)
        return f"Stopped {recipe['name']}"

    async def logs(self, recipe: dict, tail: int = 500) -> str:
        if recipe.get("runtime") == "process":
            text = self.procs.logs(recipe["container_name"], tail)
            if text is None:
                raise ActionError("No logs yet", 404)
            return text
        res = await self.docker.logs(recipe["container_name"], tail=tail)
        if not res.ok:
            raise ActionError(res.error or "No logs", 404)
        # docker logs splits the container's stdout/stderr; merge them back in timestamp order.
        lines = res.stdout.splitlines() + res.stderr.splitlines()
        return "\n".join(sorted(lines, key=lambda line: line.split(" ", 1)[0])) + "\n"

    def follow_logs(self, recipe: dict, tail: int = 200) -> AsyncIterator[str]:
        if recipe.get("runtime") == "process":
            return self.procs.follow_logs(recipe["container_name"], tail)
        return self.docker.follow_logs(recipe["container_name"], tail)

    async def images(self) -> list[dict[str, Any]]:
        try:
            return await self.docker.list_images()
        except RuntimeError as e:
            raise ActionError(f"Cannot list Docker images: {e}", 502)

    async def check_models(self, items: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Host-side existence check for resolved model targets (see resolve_model_target)."""
        return [check_model_target(it, config.MODEL_DIRS) for it in items]

    async def scan_weights(self) -> dict[str, Any]:
        """Model folders and .gguf files in this host's model dirs (DGXM_MODEL_DIR)."""
        found = await asyncio.to_thread(scan_weight_dirs, config.MODEL_DIRS)
        return {"dirs": config.MODEL_DIRS, "found": found}

    async def drop_caches(self) -> tuple[bool, str]:
        if sys.platform == "darwin":
            # macOS has no drop_caches, and a privileged container would only touch Docker's VM.
            return True, "Page cache drop is not available on macOS (skipped)"
        err = await try_drop_caches_locally()
        if err is None:
            return True, "Dropped page cache"
        res = await self.docker.drop_caches()
        if res.ok:
            return True, "Dropped page cache (via privileged container)"
        return False, f"Could not drop page cache: {res.error}"


class RemoteExecutor:
    """Talks to the agent API (/agent/v1/...) of another machine running this app."""

    def __init__(self, host: dict, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.host = host
        self.base = host["url"].rstrip("/")
        self.headers = {"Authorization": f"Bearer {host.get('token') or ''}"}
        self.transport = transport

    def _client(self, timeout: Optional[float]) -> httpx.AsyncClient:
        # Fail fast when a host is down, however long the request itself may take.
        return httpx.AsyncClient(base_url=self.base, headers=self.headers, transport=self.transport,
                                 timeout=httpx.Timeout(timeout, connect=3.0))

    async def _call(self, method: str, path: str, body: Any = None, timeout: float = 15.0) -> Any:
        try:
            async with self._client(timeout) as client:
                r = await client.request(method, f"/agent/v1{path}", json=body)
        except httpx.HTTPError as e:
            raise ActionError(f"Host '{self.host['name']}' is not reachable ({type(e).__name__}: {e})", 502)
        if r.status_code == 401:
            raise ActionError(f"Host '{self.host['name']}' rejected the agent token", 502)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ActionError(str(detail), r.status_code if r.status_code < 500 else 502)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    async def info(self) -> dict[str, Any]:
        return await self._call("GET", "/info", timeout=3.0)

    async def system(self) -> dict[str, Any]:
        return await self._call("GET", "/system", timeout=5.0)

    async def statuses(self, recipes: list[dict]) -> dict[int, dict[str, Any]]:
        data = await self._call("POST", "/statuses", {"recipes": recipes}, timeout=10.0)
        return {int(k): v for k, v in data.items()}

    async def start(self, recipe: dict) -> str:
        # Starting may pull an image; be patient.
        return (await self._call("POST", "/start", {"recipe": recipe}, timeout=1800.0))["message"]

    async def stop(self, recipe: dict) -> str:
        return (await self._call("POST", "/stop", {"recipe": recipe}, timeout=config.STOP_TIMEOUT + 60.0))["message"]

    async def logs(self, recipe: dict, tail: int = 500) -> str:
        return await self._call("POST", "/logs", {"recipe": recipe, "tail": tail}, timeout=30.0)

    async def follow_logs(self, recipe: dict, tail: int = 200) -> AsyncIterator[str]:
        try:
            async with self._client(None) as client:
                async with client.stream("POST", "/agent/v1/logs/stream",
                                         json={"recipe": recipe, "tail": tail}) as r:
                    if r.status_code >= 400:
                        yield f"[manager] host '{self.host['name']}' answered {r.status_code}"
                        return
                    async for line in r.aiter_lines():
                        yield line
        except httpx.HTTPError as e:
            yield f"[manager] lost connection to host '{self.host['name']}': {e}"

    async def images(self) -> list[dict[str, Any]]:
        try:
            return await self._call("GET", "/images", timeout=30.0)
        except ActionError as e:
            if e.status_code == 404:  # agents before this endpoint existed
                raise ActionError(f"The agent on '{self.host['name']}' is too old to list images - update it", 502)
            raise

    async def check_models(self, items: list[dict[str, str]]) -> list[dict[str, Any]]:
        if not items:
            return []
        try:
            return await self._call("POST", "/models/check", {"items": items}, timeout=30.0)
        except ActionError as e:
            if e.status_code == 404:  # agents before this endpoint existed
                raise ActionError(f"The agent on '{self.host['name']}' is too old to check models - update it", 502)
            raise

    async def scan_weights(self) -> dict[str, Any]:
        try:
            return await self._call("GET", "/weights/scan", timeout=60.0)
        except ActionError as e:
            if e.status_code == 404:  # agents before this endpoint existed
                raise ActionError(f"The agent on '{self.host['name']}' is too old to search for weights - update it", 502)
            raise

    async def drop_caches(self) -> tuple[bool, str]:
        data = await self._call("POST", "/drop-caches", timeout=300.0)
        return data["ok"], data["message"]


def port_in_use(port: int) -> bool:
    """True if something accepts connections on this port (loopback, or the DGXM_PUBLISH_IP address)."""
    targets = [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
    if config.PUBLISH_IP:
        targets.append((socket.AF_INET6 if ":" in config.PUBLISH_IP else socket.AF_INET, config.PUBLISH_IP))
    for family, addr in targets:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((addr, int(port))) == 0:
                    return True
        except OSError:
            continue
    return False


def _failed_exit(status: str, manager_stopped: bool = False) -> bool:
    """`Exited (1) 3 minutes ago` -> True. 0 and 143 (SIGTERM from a stop) are not failures.
    137 (SIGKILL) counts as clean only if the manager stopped the container: an OOM-killed
    container exits with the same code, but no stop marker was written for it."""
    if not status.startswith("Exited ("):
        return False
    code = status[len("Exited ("):].split(")", 1)[0]
    if code in ("0", "143"):
        return False
    if code == "137":
        return not manager_stopped
    return True


def agent_payload_recipe(recipe: dict) -> dict:
    """The fields an agent needs to run a recipe (no host, tags, colours...)."""
    keep = ("id", "name", "kind", "runtime", "framework", "image", "model", "container_name", "host_port",
            "container_port", "command", "args", "env", "volumes", "docker_args", "gpus", "health_path")
    return {k: recipe.get(k) for k in keep}

