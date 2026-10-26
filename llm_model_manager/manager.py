"""Model lifecycle across hosts: start / stop / restart / switch recipes, with status and health.

The Manager owns the rules (port conflicts, memory check, what "switch" stops, the activity log);
executors (executor.py) do the actual work on this machine or on a remote agent.
"""

import asyncio
from typing import Any, AsyncIterator, Optional

import httpx

from .db import Database
from .docker import Docker
from .executor import RUNNING_STATES, ActionError, LocalExecutor, RemoteExecutor, agent_payload_recipe
from .processes import ProcessRunner

__all__ = ["ActionError", "Manager"]


class Manager:
    def __init__(self, db: Database, docker: Optional[Docker] = None, procs: Optional[ProcessRunner] = None,
                 remote_transport: Optional[httpx.AsyncBaseTransport] = None):
        self.db = db
        self.local = LocalExecutor(docker, procs)
        # Only for tests: route remote calls to an in-process agent app.
        self.remote_transport = remote_transport
        # Serialises mutating operations so the scheduler and UI don't race.
        self.lock = asyncio.Lock()

    # ---- hosts ---------------------------------------------------------

    def executor_for(self, host_id: int) -> LocalExecutor | RemoteExecutor:
        host = self.db.get_host(host_id)
        if host is None:
            raise ActionError(f"Host #{host_id} does not exist", 404)
        if not host["url"]:
            return self.local
        return RemoteExecutor(host, self.remote_transport)

    def _executor(self, recipe: dict) -> LocalExecutor | RemoteExecutor:
        return self.executor_for(recipe["host_id"])

    async def host_overview(self) -> list[dict[str, Any]]:
        """Every host with online state, platform info and memory/GPU readings (never the token)."""

        async def one(host: dict) -> dict[str, Any]:
            out = {"id": host["id"], "name": host["name"], "url": host["url"], "is_local": not host["url"],
                   "has_token": bool(host["token"]), "online": False, "info": None, "system": None, "error": None}
            try:
                ex = self.executor_for(host["id"])
                out["info"], out["system"] = await asyncio.gather(ex.info(), ex.system())
                out["online"] = True
            except ActionError as e:
                out["error"] = e.message
            return out

        return list(await asyncio.gather(*(one(h) for h in self.db.list_hosts())))

    # ---- status --------------------------------------------------------

    async def _statuses(self, recipes: list[dict]) -> dict[int, dict[str, Any]]:
        """Status per recipe id, asking each host once. Unreachable hosts give status "offline"."""
        by_host: dict[int, list[dict]] = {}
        for r in recipes:
            by_host.setdefault(r["host_id"], []).append(r)

        async def one(host_id: int, rs: list[dict]) -> dict[int, dict[str, Any]]:
            try:
                return await self.executor_for(host_id).statuses([agent_payload_recipe(r) for r in rs])
            except ActionError as e:
                return {r["id"]: {"status": "offline", "container": None, "error": e.message} for r in rs}

        out: dict[int, dict[str, Any]] = {}
        for part in await asyncio.gather(*(one(h, rs) for h, rs in by_host.items())):
            out.update(part)
        return out

    async def recipes_with_status(self) -> list[dict[str, Any]]:
        recipes = self.db.list_recipes()
        statuses = await self._statuses(recipes)
        for r in recipes:
            r.update(statuses.get(r["id"], {"status": "offline", "container": None}))
        return recipes

    async def recipe_status(self, recipe_id: int) -> dict[str, Any]:
        recipe = self._get(recipe_id)
        recipe.update((await self._statuses([recipe]))[recipe_id])
        return recipe

    def _get(self, recipe_id: int) -> dict:
        recipe = self.db.get_recipe(recipe_id)
        if not recipe:
            raise ActionError("Recipe not found", 404)
        return recipe

    # ---- logs ----------------------------------------------------------

    async def logs(self, recipe: dict, tail: int = 500) -> str:
        return await self._executor(recipe).logs(agent_payload_recipe(recipe), tail)

    def follow_logs(self, recipe: dict, tail: int = 200) -> AsyncIterator[str]:
        return self._executor(recipe).follow_logs(agent_payload_recipe(recipe), tail)

    # ---- actions -------------------------------------------------------

    async def start(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        async with self.lock:
            return await self._start(self._get(recipe_id), source, force)

    async def stop(self, recipe_id: int, source: str = "user") -> str:
        async with self.lock:
            return await self._stop(self._get(recipe_id), source)

    async def restart(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        """Stop, remove and re-create the container/process so edits to the recipe take effect."""
        async with self.lock:
            recipe = self._get(recipe_id)
            await self._stop(recipe, source, record=False)
            return await self._start(recipe, source, force, action="restart")

    async def stop_all(self, source: str = "user", except_id: Optional[int] = None,
                       host_id: Optional[int] = None) -> str:
        async with self.lock:
            return await self._stop_all(source, except_id, host_id)

    async def switch(self, recipe_id: int, source: str = "user", drop_caches: bool = True) -> str:
        """Stop every other model on the same host, free memory there, then start this one."""
        async with self.lock:
            recipe = self._get(recipe_id)
            msgs = [await self._stop_all(source, except_id=recipe_id, host_id=recipe["host_id"])]
            if drop_caches:
                msgs.append(await self._drop_caches(source, recipe["host_id"]))
            msgs.append(await self._start(recipe, source, force=False, action="switch"))
            return "; ".join(m for m in msgs if m)

    async def free_memory(self, source: str = "user", host_id: Optional[int] = None) -> str:
        """Stop all models and drop the page cache, on one host or (host_id None) on all of them."""
        async with self.lock:
            msgs = [await self._stop_all(source, host_id=host_id)]
            host_ids = [host_id] if host_id is not None else [h["id"] for h in self.db.list_hosts()]
            for hid in host_ids:
                msgs.append(await self._drop_caches(source, hid))
            return "; ".join(m for m in msgs if m)

    # ---- internals (caller holds the lock) -----------------------------

    def _host_name(self, host_id: int) -> str:
        host = self.db.get_host(host_id)
        return host["name"] if host else f"#{host_id}"

    def _target(self, recipe: dict) -> str:
        if len(self.db.list_hosts()) > 1:
            return f"{self._host_name(recipe['host_id'])}: {recipe['container_name']}"
        return recipe["container_name"]

    async def _start(self, recipe: dict, source: str, force: bool, action: str = "start") -> str:
        target = self._target(recipe)
        ex = self._executor(recipe)

        def fail(msg: str, code: int):
            self.db.add_event(source, action, False, msg, recipe["id"], target)
            raise ActionError(msg, code)

        same_host = [r for r in self.db.list_recipes() if r["host_id"] == recipe["host_id"]]
        try:
            statuses = await self._statuses(same_host)
        except ActionError as e:
            fail(e.message, e.status_code)
        own = statuses[recipe["id"]]
        if own["status"] == "offline":
            fail(own.get("error") or f"Host {self._host_name(recipe['host_id'])} is offline", 502)
        if own["status"] in RUNNING_STATES:
            msg = f"{recipe['name']} is already running"
            self.db.add_event(source, action, True, msg, recipe["id"], target)
            return msg

        # Refuse to start if another recipe on the same host already holds the port.
        for other in same_host:
            if other["id"] != recipe["id"] and other["host_port"] == recipe["host_port"] \
                    and statuses[other["id"]]["status"] in RUNNING_STATES:
                fail(f"Port {recipe['host_port']} is in use by running recipe '{other['name']}'", 409)

        est = recipe.get("est_memory_gb")
        if est and not force:
            try:
                available = (await ex.system())["memory"]["available_gb"]
            except ActionError as e:
                fail(e.message, e.status_code)
            if available and est > available:
                fail(f"Not enough free memory for {recipe['name']}: needs ~{est:g} GB, "
                     f"{available:g} GB available. Stop other models or start with force.", 409)

        try:
            text = await ex.start(agent_payload_recipe(recipe))
        except ActionError as e:
            fail(e.message, e.status_code)
        self.db.add_event(source, action, True, text, recipe["id"], target)
        return text.split("\n", 1)[0]

    async def _stop(self, recipe: dict, source: str, record: bool = True) -> str:
        target = self._target(recipe)
        status = (await self._statuses([recipe]))[recipe["id"]]
        if status["status"] not in RUNNING_STATES:
            msg = f"{recipe['name']} is not running"
            if status["status"] == "offline":
                msg = status.get("error") or f"Host {self._host_name(recipe['host_id'])} is offline"
            if record:
                self.db.add_event(source, "stop", status["status"] != "offline", msg, recipe["id"], target)
            return msg
        try:
            msg = await self._executor(recipe).stop(agent_payload_recipe(recipe))
        except ActionError as e:
            self.db.add_event(source, "stop", False, e.message, recipe["id"], target)
            raise
        if record:
            self.db.add_event(source, "stop", True, msg, recipe["id"], target)
        return msg

    async def _stop_all(self, source: str, except_id: Optional[int] = None, host_id: Optional[int] = None) -> str:
        recipes = [r for r in self.db.list_recipes() if host_id is None or r["host_id"] == host_id]
        statuses = await self._statuses(recipes)
        stopped = []
        for recipe in recipes:
            if recipe["id"] != except_id and statuses[recipe["id"]]["status"] in RUNNING_STATES:
                await self._stop(recipe, source)
                stopped.append(recipe["name"])
        where = f" on {self._host_name(host_id)}" if host_id is not None else ""
        return f"Stopped {', '.join(stopped)}" if stopped else f"No other models running{where}"

    async def _drop_caches(self, source: str, host_id: int) -> str:
        name = self._host_name(host_id)
        try:
            ok, msg = await self.executor_for(host_id).drop_caches()
        except ActionError as e:
            ok, msg = False, e.message
        if len(self.db.list_hosts()) > 1:
            msg = f"{name}: {msg}"
        self.db.add_event(source, "drop_caches", ok, msg, target=name)
        return msg
