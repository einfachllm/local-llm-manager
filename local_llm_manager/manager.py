"""Model lifecycle across hosts: start / stop / restart / switch recipes, with status and health.

The Manager owns the rules (port conflicts, memory check, what "switch" stops, the activity log);
executors (executor.py) do the actual work on this machine or on a remote agent.
"""

import asyncio
from collections import Counter
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx

from .db import Database
from .docker import Docker, split_image
from .executor import (RUNNING_STATES, ActionError, LocalExecutor, RemoteExecutor, agent_payload_recipe,
                       resolve_model_target)
from .processes import ProcessRunner
from .recipes import fill_weights, uses_weights

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
        # Self-metrics: (action, ok/error) -> how many times since the manager started.
        self.actions: Counter = Counter()

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

    async def images_overview(self) -> dict[str, Any]:
        """Docker images used by recipes: which recipes use each, whether each of their hosts has it locally
        (age, size), and other local tags of the same repository there - e.g. a newer nightly to switch to."""
        recipes = [r for r in self.db.list_recipes() if r.get("runtime", "docker") == "docker" and r.get("image")]

        async def fetch(host: dict) -> tuple[int, Optional[list[dict]], Optional[str]]:
            try:
                return host["id"], await self.executor_for(host["id"]).images(), None
            except ActionError as e:
                return host["id"], None, e.message

        needed = {r["host_id"] for r in recipes}
        results = await asyncio.gather(*(fetch(h) for h in self.db.list_hosts() if h["id"] in needed))
        local = {hid: imgs for hid, imgs, _ in results if imgs is not None}
        errors = {hid: err for hid, _, err in results if err}

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for r in recipes:
            repo, tag = split_image(r["image"])
            g = groups.setdefault((repo, tag), {"image": r["image"].strip(), "repository": repo, "tag": tag,
                                                "recipes": [], "host_ids": set()})
            g["recipes"].append({"id": r["id"], "name": r["name"], "host_id": r["host_id"]})
            g["host_ids"].add(r["host_id"])

        images = []
        for (repo, tag), g in sorted(groups.items()):
            hosts = []
            for hid in sorted(g.pop("host_ids")):
                if hid not in local:
                    hosts.append({"host_id": hid, "state": "unknown", "error": errors.get(hid), "other_tags": []})
                    continue
                same_repo = [i for i in local[hid] if i["repository"] == repo]
                mine = next((i for i in same_repo if i["tag"] == tag), None)
                others = sorted((i for i in same_repo if i["tag"] != tag), key=lambda i: _created(i), reverse=True)
                hosts.append({"host_id": hid, "state": "present" if mine else "missing", "local": mine,
                              "other_tags": [{**i, "newer": bool(mine) and _created(i) > _created(mine)}
                                             for i in others]})
            images.append({**g, "hosts": hosts})
        native = sum(1 for r in self.db.list_recipes() if r.get("runtime") == "process")
        return {"images": images, "native_recipes": native}

    async def models_overview(self) -> dict[str, Any]:
        """Models referenced by model recipes that are not on registered weights: which recipes use each,
        and whether each of their hosts has the weights locally (Hugging Face cache, a mounted host path,
        or a model dir)."""
        groups: dict[str, dict[str, Any]] = {}
        for r in self.db.list_recipes():
            if r.get("kind", "model") != "model" or not (r.get("model") or "").strip():
                continue
            if r.get("weights_id") and uses_weights(r):
                continue  # on registered weights: the registry above it has their per-host paths
            target = resolve_model_target(r["model"], r.get("volumes", ""))
            ref = target["reference"]
            g = groups.setdefault(ref, {"model": ref, "kind": target["kind"], "target": target,
                                         "recipes": [], "host_ids": set()})
            g["recipes"].append({"id": r["id"], "name": r["name"], "host_id": r["host_id"]})
            g["host_ids"].add(r["host_id"])

        # Build per-host item lists: one resolved target per distinct reference on that host.
        by_host: dict[int, set[str]] = {}
        for ref, g in groups.items():
            for hid in g["host_ids"]:
                by_host.setdefault(hid, set()).add(ref)

        async def fetch(host: dict) -> tuple[int, list[dict[str, Any]], Optional[str]]:
            items = [groups[ref]["target"] for ref in sorted(by_host[host["id"]])]
            try:
                return host["id"], await self.executor_for(host["id"]).check_models(items), None
            except ActionError as e:
                return host["id"], [], e.message

        results = await asyncio.gather(*(fetch(h) for h in self.db.list_hosts() if h["id"] in by_host))
        presence = {hid: {d["reference"]: d for d in data} for hid, data, _ in results if data is not None}
        errors = {hid: err for hid, _, err in results if err}

        models = []
        for ref, g in sorted(groups.items()):
            hosts = []
            for hid in sorted(g.pop("host_ids")):
                d = presence.get(hid, {}).get(ref)
                if d is None:
                    hosts.append({"host_id": hid, "state": "unknown", "error": errors.get(hid), "location": None})
                elif d["present"]:
                    hosts.append({"host_id": hid, "state": "present", "error": None, "location": d.get("location")})
                else:
                    hosts.append({"host_id": hid, "state": "missing", "error": None, "location": d.get("expected")})
            g.pop("target")  # don't serialise the internal target dict
            models.append({**g, "hosts": hosts})
        return {"models": models}

    # ---- weights -------------------------------------------------------

    def _payload(self, recipe: dict) -> dict:
        """What an executor gets: the recipe with {weights} filled in for its host (older agents need
        nothing new for this, they only see the resolved paths)."""
        path = self.db.weight_path(recipe.get("weights_id"), recipe["host_id"])
        return agent_payload_recipe(fill_weights(recipe, path))

    async def _check_paths(self, host_id: int, paths: list[str]) -> tuple[dict[str, dict], Optional[str]]:
        """path -> {present, location, expected} on one host, or ({}, error) when the host can't tell."""
        items = [{"kind": "volume", "reference": p, "host_path": p} for p in paths]
        try:
            res = await self.executor_for(host_id).check_models(items)
        except ActionError as e:
            return {}, e.message
        return {d["reference"]: d for d in res}, None

    async def check_weight(self, weight_id: int, host_id: int) -> dict[str, Any]:
        """Whether a weights entry is usable on a host: "unset" (no path there), "present", "missing" or
        "unknown" (the host could not be asked)."""
        path = self.db.weight_path(weight_id, host_id)
        if not path:
            return {"host_id": host_id, "state": "unset", "path": None, "location": None, "error": None}
        found, err = await self._check_paths(host_id, [path])
        d = found.get(path)
        if d is None:
            return {"host_id": host_id, "state": "unknown", "path": path, "location": None, "error": err}
        return {"host_id": host_id, "state": "present" if d["present"] else "missing", "path": path,
                "location": d.get("location") or d.get("expected"), "error": None}

    async def weights_overview(self) -> dict[str, Any]:
        """Registered weights with, for every host, the path set there and whether it exists."""
        weights = self.db.list_weights()
        hosts = self.db.list_hosts()

        async def fetch(host: dict) -> tuple[int, dict[str, dict], Optional[str]]:
            paths = sorted({w["locations"][host["id"]] for w in weights if host["id"] in w["locations"]})
            if not paths:
                return host["id"], {}, None
            found, err = await self._check_paths(host["id"], paths)
            return host["id"], found, err

        results = {hid: (found, err) for hid, found, err in await asyncio.gather(*(fetch(h) for h in hosts))}
        out = []
        for w in weights:
            cells = []
            for h in hosts:
                path = w["locations"].get(h["id"])
                found, err = results[h["id"]]
                d = found.get(path) if path else None
                state = "unset" if not path else "unknown" if d is None else "present" if d["present"] else "missing"
                cells.append({"host_id": h["id"], "state": state, "path": path, "error": err if state == "unknown" else None,
                              "location": (d.get("location") or d.get("expected")) if d else None})
            out.append({"id": w["id"], "name": w["name"], "source": w["source"], "recipe_ids": w["recipe_ids"],
                        "hosts": cells})
        return {"weights": out}

    # ---- status --------------------------------------------------------

    async def _statuses(self, recipes: list[dict]) -> dict[int, dict[str, Any]]:
        """Status per recipe id, asking each host once. Unreachable hosts give status "offline"."""
        by_host: dict[int, list[dict]] = {}
        for r in recipes:
            by_host.setdefault(r["host_id"], []).append(r)

        async def one(host_id: int, rs: list[dict]) -> dict[int, dict[str, Any]]:
            try:
                return await self.executor_for(host_id).statuses([self._payload(r) for r in rs])
            except ActionError as e:
                return {r["id"]: {"status": "offline", "container": None, "error": e.message} for r in rs}

        out: dict[int, dict[str, Any]] = {}
        for part in await asyncio.gather(*(one(h, rs) for h, rs in by_host.items())):
            out.update(part)
        return out

    async def recipes_with_status(self) -> list[dict[str, Any]]:
        recipes = self.db.list_recipes()
        statuses = await self._statuses(recipes)
        self._check_start_times(statuses)
        for r in recipes:
            r.update(statuses.get(r["id"], {"status": "offline", "container": None}))
        return recipes

    def _check_start_times(self, statuses: dict[int, dict[str, Any]]) -> None:
        """Close pending start measurements once the model is ready (or note that it never was)."""
        for rid, (ts, source) in self.db.pending_starts().items():
            status = statuses.get(rid, {}).get("status")
            if status == "ready":
                try:
                    seconds = (datetime.now().astimezone() - datetime.fromisoformat(ts)).total_seconds()
                except ValueError:
                    seconds = -1
                if 0 < seconds < 24 * 3600:
                    self.db.add_start_time(rid, seconds, source)
                self.db.clear_pending_start(rid)
            elif status in ("stopped", "failed", "offline"):
                # Stopped or crashed before becoming ready: drop the measurement.
                self.db.clear_pending_start(rid)

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
        return await self._executor(recipe).logs(self._payload(recipe), tail)

    def follow_logs(self, recipe: dict, tail: int = 200) -> AsyncIterator[str]:
        return self._executor(recipe).follow_logs(self._payload(recipe), tail)

    # ---- actions -------------------------------------------------------

    def _count(self, action: str, ok: bool) -> None:
        self.actions[(action, "ok" if ok else "error")] += 1

    async def start(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        async with self.lock:
            try:
                result = await self._start(self._get(recipe_id), source, force)
            except ActionError:
                self._count("start", False)
                raise
            self._count("start", True)
            return result

    async def stop(self, recipe_id: int, source: str = "user") -> str:
        async with self.lock:
            try:
                result = await self._stop(self._get(recipe_id), source)
            except ActionError:
                self._count("stop", False)
                raise
            self._count("stop", True)
            return result

    async def restart(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        """Stop, remove and re-create the container/process so edits to the recipe take effect."""
        async with self.lock:
            recipe = self._get(recipe_id)
            try:
                await self._stop(recipe, source, record=False)
                result = await self._start(recipe, source, force, action="restart")
            except ActionError:
                self._count("restart", False)
                raise
            self._count("restart", True)
            return result

    async def stop_all(self, source: str = "user", except_id: Optional[int] = None,
                       host_id: Optional[int] = None) -> str:
        async with self.lock:
            try:
                result = await self._stop_all(source, except_id, host_id)
            except ActionError:
                self._count("stop_all", False)
                raise
            self._count("stop_all", True)
            return result

    async def switch(self, recipe_id: int, source: str = "user", drop_caches: bool = True) -> str:
        """Stop every other model on the same host, free memory there, then start this one.

        Agents on the host keep running: they use a model rather than compete with it for memory.
        """
        async with self.lock:
            recipe = self._get(recipe_id)
            try:
                if recipe.get("kind") == "agent":
                    msg = f"{recipe['name']} is an agent: start it instead of switching to it"
                    self.db.add_event(source, "switch", False, msg, recipe["id"], self._target(recipe))
                    raise ActionError(msg, 409)
                msgs = [await self._stop_all(source, except_id=recipe_id, host_id=recipe["host_id"],
                                             keep_agents=True)]
                if drop_caches:
                    msgs.append(await self._drop_caches(source, recipe["host_id"]))
                msgs.append(await self._start(recipe, source, force=False, action="switch"))
                result = "; ".join(m for m in msgs if m)
            except ActionError:
                self._count("switch", False)
                raise
            self._count("switch", True)
            return result

    async def free_memory(self, source: str = "user", host_id: Optional[int] = None) -> str:
        """Stop all models and drop the page cache, on one host or (host_id None) on all of them."""
        async with self.lock:
            try:
                msgs = [await self._stop_all(source, host_id=host_id)]
                host_ids = [host_id] if host_id is not None else [h["id"] for h in self.db.list_hosts()]
                for hid in host_ids:
                    msgs.append(await self._drop_caches(source, hid))
                result = "; ".join(m for m in msgs if m)
            except ActionError:
                self._count("free_memory", False)
                raise
            self._count("free_memory", True)
            return result

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

        if uses_weights(recipe):
            await self._require_weights(recipe, fail)

        try:
            text = await ex.start(self._payload(recipe))
        except ActionError as e:
            fail(e.message, e.status_code)
        self.db.set_pending_start(recipe["id"], datetime.now().astimezone().isoformat(timespec="seconds"), source)
        self.db.add_event(source, action, True, text, recipe["id"], target)
        return text.split("\n", 1)[0]

    async def _require_weights(self, recipe: dict, fail) -> None:
        """Refuse to start a recipe whose {weights} has no path on its host, or whose path is not there."""
        host = self._host_name(recipe["host_id"])
        weight = self.db.get_weight(recipe["weights_id"]) if recipe.get("weights_id") else None
        if weight is None:
            fail(f"{recipe['name']} uses {{weights}} but has no weights selected: pick them in the recipe", 409)
        check = await self.check_weight(weight["id"], recipe["host_id"])
        if check["state"] == "unset":
            fail(f"Weights '{weight['name']}' have no path on {host}: set it in the Weights tab", 409)
        if check["state"] == "missing":
            fail(f"Weights '{weight['name']}' not found on {host} at {check['location'] or check['path']}: "
                 f"copy them there or fix the path in the Weights tab", 409)
        # "unknown" (e.g. an agent too old to check): let the container report it.

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
            msg = await self._executor(recipe).stop(self._payload(recipe))
        except ActionError as e:
            self.db.add_event(source, "stop", False, e.message, recipe["id"], target)
            raise
        if record:
            self.db.add_event(source, "stop", True, msg, recipe["id"], target)
        return msg

    async def _stop_all(self, source: str, except_id: Optional[int] = None, host_id: Optional[int] = None,
                        keep_agents: bool = False) -> str:
        recipes = [r for r in self.db.list_recipes() if (host_id is None or r["host_id"] == host_id)
                   and not (keep_agents and r.get("kind") == "agent")]
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


def _created(image: dict) -> datetime:
    """Creation time of a local image for comparisons; unknown values sort as oldest."""
    try:
        value = datetime.fromisoformat(image.get("created") or "")
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
