"""Model lifecycle: start / stop / restart / switch recipes, with status and health."""

import asyncio
import sys
from typing import Any, AsyncIterator, Optional

import httpx

from . import config, system
from .db import Database
from .docker import Docker, try_drop_caches_locally
from .processes import ProcessError, ProcessRunner
from .recipes import build_process_argv, build_run_argv, format_command, format_process_command, process_env


class ActionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class Manager:
    def __init__(self, db: Database, docker: Optional[Docker] = None,
                 procs: Optional[ProcessRunner] = None):
        self.db = db
        self.docker = docker or Docker()
        self.procs = procs or ProcessRunner(config.PROC_DIR)
        # Serialises mutating docker operations so the scheduler and UI don't race.
        self.lock = asyncio.Lock()

    # ---- status --------------------------------------------------------

    async def _healthy(self, client: httpx.AsyncClient, recipe: dict) -> bool:
        path = recipe.get("health_path") or ""
        if not path:
            return True
        try:
            r = await client.get(f"http://{config.HEALTH_HOST}:{recipe['host_port']}{path}")
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    async def _instances(self, recipes: list[dict]) -> dict[int, Optional[dict]]:
        """Container (docker) or process (native) info per recipe id, in the same shape."""
        need_docker = any(r.get("runtime", "docker") == "docker" for r in recipes)
        containers = await self.docker.list_containers() if need_docker else {}
        return {
            r["id"]: self.procs.status(r["container_name"]) if r.get("runtime") == "process"
            else containers.get(r["container_name"])
            for r in recipes
        }

    async def recipes_with_status(self) -> list[dict[str, Any]]:
        recipes = self.db.list_recipes()
        instances = await self._instances(recipes)
        running = []
        for r in recipes:
            c = instances[r["id"]]
            r["container"] = c
            if c is None:
                r["status"] = "stopped"
            elif c["state"] == "running":
                r["status"] = "running"
                running.append(r)
            elif c["state"] in ("created", "restarting"):
                r["status"] = "starting"
            elif r.get("runtime") == "process":
                r["status"] = "failed" if c.get("failed") else "stopped"
            else:
                r["status"] = "failed" if _failed_exit(c["status"]) else "stopped"
        if running:
            async with httpx.AsyncClient(timeout=2.0) as client:
                health = await asyncio.gather(*(self._healthy(client, r) for r in running))
            for r, ok in zip(running, health):
                r["status"] = "ready" if ok else "loading"
        return recipes

    async def recipe_status(self, recipe_id: int) -> dict[str, Any]:
        for r in await self.recipes_with_status():
            if r["id"] == recipe_id:
                return r
        raise ActionError("Recipe not found", 404)

    def _get(self, recipe_id: int) -> dict:
        recipe = self.db.get_recipe(recipe_id)
        if not recipe:
            raise ActionError("Recipe not found", 404)
        return recipe

    # ---- logs ----------------------------------------------------------

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

    # ---- actions -------------------------------------------------------

    async def start(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        async with self.lock:
            return await self._start(self._get(recipe_id), source, force)

    async def stop(self, recipe_id: int, source: str = "user") -> str:
        async with self.lock:
            return await self._stop(self._get(recipe_id), source)

    async def restart(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        """Stop, remove and re-create the container so edits to the recipe take effect."""
        async with self.lock:
            recipe = self._get(recipe_id)
            await self._stop(recipe, source, record=False)
            return await self._start(recipe, source, force, action="restart")

    async def stop_all(self, source: str = "user", except_id: Optional[int] = None) -> str:
        async with self.lock:
            return await self._stop_all(source, except_id)

    async def switch(self, recipe_id: int, source: str = "user", drop_caches: bool = True) -> str:
        """Stop every other managed model, free memory, then start this one."""
        async with self.lock:
            recipe = self._get(recipe_id)
            msgs = [await self._stop_all(source, except_id=recipe_id)]
            if drop_caches:
                msgs.append(await self._drop_caches(source))
            msgs.append(await self._start(recipe, source, force=False, action="switch"))
            return "; ".join(m for m in msgs if m)

    async def free_memory(self, source: str = "user") -> str:
        async with self.lock:
            msgs = [await self._stop_all(source), await self._drop_caches(source)]
            return "; ".join(m for m in msgs if m)

    # ---- internals (caller holds the lock) -----------------------------

    async def _start(self, recipe: dict, source: str, force: bool, action: str = "start") -> str:
        name = recipe["container_name"]
        all_recipes = self.db.list_recipes()
        instances = await self._instances(all_recipes)
        existing = instances.get(recipe["id"])
        if existing and existing["state"] == "running":
            msg = f"{recipe['name']} is already running"
            self.db.add_event(source, action, True, msg, recipe["id"], name)
            return msg

        # Refuse to start if another managed recipe already holds the host port.
        for other in all_recipes:
            if other["id"] == recipe["id"] or other["host_port"] != recipe["host_port"]:
                continue
            c = instances.get(other["id"])
            if c and c["state"] == "running":
                msg = f"Port {recipe['host_port']} is in use by running recipe '{other['name']}'"
                self.db.add_event(source, action, False, msg, recipe["id"], name)
                raise ActionError(msg, 409)

        est = recipe.get("est_memory_gb")
        if est and not force:
            available = system.read_meminfo()["available_gb"]
            if available and est > available:
                msg = (f"Not enough free memory for {recipe['name']}: needs ~{est:g} GB, "
                       f"{available:g} GB available. Stop other models or start with force.")
                self.db.add_event(source, action, False, msg, recipe["id"], name)
                raise ActionError(msg, 409)

        if recipe.get("runtime") == "process":
            argv = build_process_argv(recipe)
            try:
                pid = self.procs.start(name, argv, process_env(recipe))
            except (ProcessError, OSError) as e:
                msg = f"Could not start process: {e}"
                self.db.add_event(source, action, False, msg, recipe["id"], name)
                raise ActionError(msg, 500)
            msg = f"Started {recipe['name']} (pid {pid})"
            self.db.add_event(source, action, True, msg + "\n" + format_process_command(recipe), recipe["id"], name)
            return msg

        if existing:
            await self.docker.remove(name)

        argv = build_run_argv(recipe)
        res = await self.docker.run_argv(argv)
        if not res.ok:
            msg = f"docker run failed: {res.error}"
            self.db.add_event(source, action, False, msg, recipe["id"], name)
            raise ActionError(msg, 500)
        msg = f"Started {recipe['name']} ({res.stdout.strip()[:12]})"
        self.db.add_event(source, action, True, msg + "\n" + format_command(argv), recipe["id"], name)
        return msg

    async def _stop(self, recipe: dict, source: str, record: bool = True) -> str:
        name = recipe["container_name"]
        c = (await self._instances([recipe]))[recipe["id"]]
        if not c or c["state"] != "running":
            msg = f"{recipe['name']} is not running"
            if record:
                self.db.add_event(source, "stop", True, msg, recipe["id"], name)
            return msg
        if recipe.get("runtime") == "process":
            await self.procs.stop(name)
            msg = f"Stopped {recipe['name']}"
            if record:
                self.db.add_event(source, "stop", True, msg, recipe["id"], name)
            return msg
        res = await self.docker.stop(name)
        if not res.ok:
            msg = f"docker stop failed: {res.error}"
            self.db.add_event(source, "stop", False, msg, recipe["id"], name)
            raise ActionError(msg, 500)
        msg = f"Stopped {recipe['name']}"
        if record:
            self.db.add_event(source, "stop", True, msg, recipe["id"], name)
        return msg

    async def _stop_all(self, source: str, except_id: Optional[int] = None) -> str:
        recipes = self.db.list_recipes()
        instances = await self._instances(recipes)
        stopped = []
        for recipe in recipes:
            if recipe["id"] == except_id:
                continue
            c = instances.get(recipe["id"])
            if c and c["state"] == "running":
                await self._stop(recipe, source)
                stopped.append(recipe["name"])
        return f"Stopped {', '.join(stopped)}" if stopped else "No other models running"

    async def _drop_caches(self, source: str) -> str:
        if sys.platform == "darwin":
            # macOS has no drop_caches, and a privileged container would only touch Docker's VM.
            msg = "Page cache drop is not available on macOS (skipped)"
            self.db.add_event(source, "drop_caches", True, msg)
            return msg
        err = await try_drop_caches_locally()
        if err is None:
            msg, ok = "Dropped page cache", True
        else:
            res = await self.docker.drop_caches()
            ok = res.ok
            msg = "Dropped page cache (via privileged container)" if ok else f"Could not drop page cache: {res.error}"
        self.db.add_event(source, "drop_caches", ok, msg)
        return msg


def _failed_exit(status: str) -> bool:
    """`Exited (1) 3 minutes ago` -> True. 0 and the codes docker stop produces (137/143) are not failures."""
    if not status.startswith("Exited ("):
        return False
    code = status[len("Exited ("):].split(")", 1)[0]
    return code not in ("0", "137", "143")
