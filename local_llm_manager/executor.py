"""Where recipes actually run: on this machine (LocalExecutor) or on an agent (RemoteExecutor).

The hub keeps recipes, schedules and the activity log; executors only run what they are given.
Both executors have the same async interface, so the Manager does not care where a host is.
"""

import asyncio
import platform
import socket
import sys
from typing import Any, AsyncIterator, Optional

import httpx

from . import __version__, config, logstats, servermetrics, system
from .docker import Docker, try_drop_caches_locally
from .processes import ProcessError, ProcessRunner
from .recipes import build_process_argv, build_run_argv, format_command, format_process_command, process_env


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
                status = "failed" if _failed_exit(c["status"]) else "stopped"
            out[r["id"]] = {"status": status, "container": c}
        if running:
            async with httpx.AsyncClient(timeout=2.0) as client:
                health = await asyncio.gather(*(self._healthy(client, r) for r in running))
                stats = await asyncio.gather(*(self._metrics(client, r, instances[r["id"]]) for r in running))
            for r, ok, m in zip(running, health, stats):
                out[r["id"]]["status"] = "ready" if ok else "loading"
                if m:
                    out[r["id"]]["metrics"] = m
        self.rates.forget_except({_instance_key(r, instances[r["id"]]) for r in running})
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
        res = await self.docker.stop(name)
        if not res.ok:
            raise ActionError(f"docker stop failed: {res.error}", 500)
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


def _failed_exit(status: str) -> bool:
    """`Exited (1) 3 minutes ago` -> True. 0 and the codes docker stop produces (137/143) are not failures."""
    if not status.startswith("Exited ("):
        return False
    code = status[len("Exited ("):].split(")", 1)[0]
    return code not in ("0", "137", "143")


def agent_payload_recipe(recipe: dict) -> dict:
    """The fields an agent needs to run a recipe (no host, tags, colours...)."""
    keep = ("id", "name", "runtime", "framework", "image", "model", "container_name", "host_port",
            "container_port", "command", "args", "env", "volumes", "docker_args", "gpus", "health_path")
    return {k: recipe.get(k) for k in keep}

