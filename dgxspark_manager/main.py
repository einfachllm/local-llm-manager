"""FastAPI application: REST API + static web UI."""

import base64
import logging
import os
import re
import secrets
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import __version__, config, system
from .db import Database, normalize_tags
from .docker import Docker
from .importer import ScriptImportError, import_script
from .script_check import check_script
from .manager import ActionError, Manager
from .processes import ProcessRunner
from .agent import create_agent_app
from .recipes import (CARD_COLORS, FRAMEWORKS, RUNTIMES, build_run_argv, format_command, format_process_command,
                      split_args, uses_host_network)
from .scheduler import ACTIONS, Scheduler, compute_next_run, local_now, parse_time, validate_cron
from .timeline import build_timeline

STATIC_DIR = Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class RecipeIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    host_id: Optional[int] = None  # None = this machine
    runtime: str = "docker"
    framework: str = "vllm"
    image: str = ""
    model: str = ""
    container_name: str = ""
    host_port: int = Field(ge=1, le=65535)
    container_port: int = Field(ge=1, le=65535)
    command: str = ""
    args: str = ""
    env: str = ""
    volumes: str = ""
    docker_args: str = ""
    gpus: str = "all"
    health_path: str = "/health"
    est_memory_gb: Optional[float] = Field(default=None, ge=0)
    tags: list[str] = []
    color: str = ""

    @field_validator("color")
    @classmethod
    def known_color(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CARD_COLORS:
            raise ValueError(f"color must be one of {', '.join(c for c in CARD_COLORS if c)} or empty")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, v):
        return normalize_tags(v)

    @field_validator("name", "image", "model", "container_name", "gpus", "health_path")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("framework")
    @classmethod
    def known_framework(cls, v: str) -> str:
        if v not in FRAMEWORKS:
            raise ValueError(f"framework must be one of {', '.join(FRAMEWORKS)}")
        return v

    @model_validator(mode="after")
    def check_runtime(self):
        if self.runtime not in RUNTIMES:
            raise ValueError(f"runtime must be one of {', '.join(RUNTIMES)}")
        if FRAMEWORKS[self.framework]["runtime"] != self.runtime:
            raise ValueError(f"framework '{self.framework}' is not a {RUNTIMES[self.runtime]} framework")
        if self.runtime == "docker" and not self.image:
            raise ValueError("image is required for Docker recipes")
        if self.runtime == "process":
            if not self.command.strip():
                raise ValueError("command is required for native-process recipes")
            # The server listens on the host directly: one port, and the docker-only fields are unused.
            self.container_port = self.host_port
        return self

    @model_validator(mode="after")
    def derive_container_name(self):
        if not self.container_name:
            self.container_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.name).strip("-._").lower() or "model"
        if not NAME_RE.match(self.container_name):
            raise ValueError("container_name may only contain letters, digits, '_', '.', '-'")
        if self.health_path and not self.health_path.startswith("/"):
            self.health_path = "/" + self.health_path
        try:
            host_network = uses_host_network(split_args(self.docker_args))
        except ValueError:
            raise ValueError("Extra docker flags: unbalanced quotes")
        if host_network:
            # The server listens on the host directly, so there is only one port.
            self.container_port = self.host_port
        return self


class ImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    # Required when the static check finds risky calls.
    trust: bool = False


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    action: str
    recipe_id: Optional[int] = None
    cron: Optional[str] = None
    run_at: Optional[str] = None
    enabled: bool = True
    host_id: Optional[int] = None  # stop_all / free_memory: one host, or None for all hosts

    @model_validator(mode="after")
    def check(self):
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        if ACTIONS[self.action]["needs_recipe"]:
            if self.recipe_id is None:
                raise ValueError("this action needs a recipe")
        else:
            self.recipe_id = None
        if ACTIONS[self.action]["needs_recipe"]:
            self.host_id = None  # the recipe's host is used
        self.cron = (self.cron or "").strip() or None
        self.run_at = (self.run_at or "").strip() or None
        if bool(self.cron) == bool(self.run_at):
            raise ValueError("set exactly one of cron or run_at")
        if self.cron:
            validate_cron(self.cron)
        else:
            try:
                parse_time(self.run_at)
            except ValueError:
                raise ValueError("run_at must be an ISO date-time, e.g. 2026-10-25T22:00")
        return self


class HostIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = ""     # empty for this machine; e.g. http://mac-studio.local:8090 for an agent
    token: str = ""   # agent token; empty on update = keep the stored one

    @model_validator(mode="after")
    def check(self):
        self.name, self.url, self.token = self.name.strip(), self.url.strip().rstrip("/"), self.token.strip()
        if self.url and not re.match(r"^https?://[^\s/]+(:\d+)?(/.*)?$", self.url):
            raise ValueError("url must look like http://hostname:8090")
        return self


def seed_examples(db: Database) -> None:
    if db.list_recipes():
        return
    if sys.platform == "darwin":
        seed_mac_examples(db)
        return
    vllm = FRAMEWORKS["vllm"]
    db.create_recipe({
        "name": "Qwen3-Coder-30B-AWQ (vLLM)",
        "description": "Example recipe - edit to match your setup.",
        "framework": "vllm", "image": vllm["image"],
        "model": "cpatonn/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
        "container_name": "vllm-qwen3-coder-30b-awq", "host_port": 8104, "container_port": 8000,
        "command": vllm["command"],
        "args": ("--max-model-len 65536\n--max-num-seqs 8\n--gpu-memory-utilization 0.4\n"
                 "--enable-prefix-caching\n--enable-auto-tool-choice\n--tool-call-parser qwen3_coder"),
        "env": vllm["env"], "volumes": vllm["volumes"], "docker_args": vllm["docker_args"],
        "gpus": "all", "health_path": "/health", "est_memory_gb": 45, "tags": ["qwen3-coder", "example"],
    })
    sg = FRAMEWORKS["sglang"]
    db.create_recipe({
        "name": "Qwen3-8B (SGLang)",
        "description": "Example recipe - edit to match your setup.",
        "framework": "sglang", "image": sg["image"], "model": "Qwen/Qwen3-8B",
        "container_name": "sglang-qwen3-8b", "host_port": 8110, "container_port": 30000,
        "command": sg["command"], "args": sg["args"], "env": sg["env"], "volumes": sg["volumes"],
        "docker_args": sg["docker_args"], "gpus": "all", "health_path": "/health", "est_memory_gb": 30,
        "tags": ["qwen3-8b", "example"],
    })


def seed_mac_examples(db: Database) -> None:
    mlx = FRAMEWORKS["mlx"]
    db.create_recipe({
        "name": "Qwen3-8B (MLX)", "description": "Example recipe - needs `pip install mlx-lm`.",
        "runtime": "process", "framework": "mlx", "image": "", "model": "mlx-community/Qwen3-8B-4bit",
        "container_name": "mlx-qwen3-8b", "host_port": 8080, "container_port": 8080,
        "command": mlx["command"], "args": "", "env": "", "volumes": "", "docker_args": "", "gpus": "",
        "health_path": mlx["health_path"], "est_memory_gb": 6, "tags": ["qwen3-8b", "example"],
    })
    llama = FRAMEWORKS["llamacpp-native"]
    db.create_recipe({
        "name": "GGUF model (llama.cpp)", "description": "Example recipe - needs `brew install llama.cpp`.",
        "runtime": "process", "framework": "llamacpp-native", "image": "", "model": "~/models/model.gguf",
        "container_name": "llamacpp-gguf", "host_port": 8081, "container_port": 8081,
        "command": llama["command"], "args": llama["args"], "env": "", "volumes": "", "docker_args": "",
        "gpus": "", "health_path": llama["health_path"], "est_memory_gb": None, "tags": ["gguf", "example"],
    })


def create_app(db: Optional[Database] = None, docker: Optional[Docker] = None,
               run_scheduler: bool = True, procs: Optional[ProcessRunner] = None,
               remote_transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    db = db or Database(config.DB_PATH)
    if os.environ.get("DGXM_SEED", "1") != "0":
        seed_examples(db)
    manager = Manager(db, docker, procs, remote_transport)
    scheduler = Scheduler(db, manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_scheduler:
            scheduler.start()
        yield
        await scheduler.stop()

    app = FastAPI(title="DGX Spark Manager", version=__version__, lifespan=lifespan)
    app.state.db, app.state.manager, app.state.scheduler = db, manager, scheduler

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if config.AUTH_PASSWORD:
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user, config.AUTH_USER) and secrets.compare_digest(pw, config.AUTH_PASSWORD)
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="DGX Spark Manager"'})
        return await call_next(request)

    @app.exception_handler(ActionError)
    async def action_error(_: Request, exc: ActionError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(_: Request, exc: sqlite3.IntegrityError):
        msg = str(exc)
        if "recipes.name" in msg:
            msg = "A recipe with this name already exists"
        elif "recipes.container_name" in msg:
            msg = "Another recipe already uses this container name"
        elif "hosts.name" in msg:
            msg = "A host with this name already exists"
        return JSONResponse(status_code=409, content={"detail": msg})

    def get_recipe_or_404(recipe_id: int) -> dict:
        r = db.get_recipe(recipe_id)
        if not r:
            raise HTTPException(404, "Recipe not found")
        return r

    # ---- meta ----------------------------------------------------------

    @app.get("/api/meta")
    async def meta():
        return {
            "version": __version__,
            "frameworks": FRAMEWORKS,
            "colors": CARD_COLORS,
            "runtimes": RUNTIMES,
            "platform": sys.platform,
            "actions": ACTIONS,
        }

    @app.get("/api/system")
    async def system_info():
        return await system.snapshot()

    @app.post("/api/system/free-memory")
    async def free_memory(host_id: Optional[int] = None):
        if host_id is not None:
            get_host_or_404(host_id)
        return {"message": await manager.free_memory(host_id=host_id)}

    @app.post("/api/system/stop-all")
    async def stop_all(host_id: Optional[int] = None):
        if host_id is not None:
            get_host_or_404(host_id)
        return {"message": await manager.stop_all(host_id=host_id)}

    # ---- hosts ---------------------------------------------------------

    def get_host_or_404(host_id: int) -> dict:
        h = db.get_host(host_id)
        if not h:
            raise HTTPException(404, "Host not found")
        return h

    @app.get("/api/hosts")
    async def list_hosts():
        """All hosts with online state and memory/GPU readings. Tokens are never returned."""
        return await manager.host_overview()

    @app.post("/api/hosts", status_code=201)
    async def create_host(body: HostIn):
        if not body.url:
            raise HTTPException(422, "url is required (this machine is already listed)")
        if not body.token:
            raise HTTPException(422, "token is required: the agent's DGXM_AGENT_TOKEN")
        hid = db.create_host(body.name, body.url, body.token)
        return next(h for h in await manager.host_overview() if h["id"] == hid)

    @app.put("/api/hosts/{host_id}")
    async def update_host(host_id: int, body: HostIn):
        host = get_host_or_404(host_id)
        if not host["url"] and body.url:
            raise HTTPException(422, "This machine has no URL; add other machines as new hosts")
        if host["url"] and not body.url:
            raise HTTPException(422, "url is required for a remote host")
        db.update_host(host_id, body.name, body.url, body.token or host["token"])
        return next(h for h in await manager.host_overview() if h["id"] == host_id)

    @app.delete("/api/hosts/{host_id}", status_code=204)
    async def delete_host(host_id: int):
        host = get_host_or_404(host_id)
        if not host["url"]:
            raise HTTPException(409, "This machine cannot be removed")
        n = db.count_host_recipes(host_id)
        if n:
            raise HTTPException(409, f"Move or delete the {n} recipe(s) on this host first")
        db.delete_host(host_id)
        return Response(status_code=204)

    # ---- recipes -------------------------------------------------------

    @app.get("/api/recipes")
    async def list_recipes():
        return await manager.recipes_with_status()

    @app.get("/api/recipes/{recipe_id}")
    async def get_recipe(recipe_id: int):
        return await manager.recipe_status(recipe_id)

    def recipe_data(body: RecipeIn) -> dict:
        data = body.model_dump()
        data["host_id"] = get_host_or_404(body.host_id)["id"] if body.host_id else db.local_host_id()
        return data

    @app.post("/api/recipes", status_code=201)
    async def create_recipe(body: RecipeIn):
        rid = db.create_recipe(recipe_data(body))
        return db.get_recipe(rid)

    @app.put("/api/recipes/{recipe_id}")
    async def update_recipe(recipe_id: int, body: RecipeIn):
        old = get_recipe_or_404(recipe_id)
        data = recipe_data(body)
        status = await manager.recipe_status(recipe_id)
        running = status["status"] in ("ready", "loading", "starting")
        if running and old["container_name"] != body.container_name:
            raise HTTPException(409, "Stop the model before renaming its container")
        if running and old["host_id"] != data["host_id"]:
            raise HTTPException(409, "Stop the model before moving it to another host")
        db.update_recipe(recipe_id, data)
        return db.get_recipe(recipe_id)

    @app.delete("/api/recipes/{recipe_id}", status_code=204)
    async def delete_recipe(recipe_id: int):
        get_recipe_or_404(recipe_id)
        status = await manager.recipe_status(recipe_id)
        if status["status"] in ("ready", "loading", "starting"):
            raise HTTPException(409, "Stop the model before deleting its recipe")
        db.delete_recipe(recipe_id)
        return Response(status_code=204)

    @app.post("/api/recipes/{recipe_id}/duplicate", status_code=201)
    async def duplicate_recipe(recipe_id: int):
        r = get_recipe_or_404(recipe_id)
        names = {x["name"] for x in db.list_recipes()}
        containers = {x["container_name"] for x in db.list_recipes()}
        i = 1
        while f"{r['name']} (copy {i})" in names or f"{r['container_name']}-copy{i}" in containers:
            i += 1
        r["name"] = f"{r['name']} (copy {i})"
        r["container_name"] = f"{r['container_name']}-copy{i}"
        return db.get_recipe(db.create_recipe(r))

    @app.post("/api/recipes/preview")
    async def preview(body: RecipeIn):
        recipe = body.model_dump()
        if body.runtime == "process":
            return {"command": format_process_command(recipe)}
        return {"command": format_command(build_run_argv(recipe))}

    @app.post("/api/recipes/import/check")
    async def check_import(body: ImportIn):
        """Static review of a script before importing it. Nothing is executed."""
        return check_script(body.text)

    @app.post("/api/recipes/import")
    async def import_recipe(body: ImportIn):
        """Parse a starter script / docker run command into an unsaved recipe draft."""
        check = check_script(body.text)
        if check["count"] and not body.trust:
            raise HTTPException(422, "The script contains risky calls. Review them and confirm that you trust the script.")
        try:
            result = await import_script(body.text)
        except ScriptImportError as e:
            raise HTTPException(422, str(e))
        recipe = result["recipe"]
        existing = db.list_recipes()
        names = {r["name"] for r in existing}
        containers = {r["container_name"] for r in existing}
        base, i = recipe["name"], 2
        while recipe["name"] in names:
            recipe["name"], i = f"{base} ({i})", i + 1
        if recipe["container_name"] in containers:
            base, i = recipe["container_name"], 2
            while recipe["container_name"] in containers:
                recipe["container_name"], i = f"{base}-{i}", i + 1
            result["warnings"].append(f"Container name changed to {recipe['container_name']} (already used)")
        result["check"] = check
        if check["count"]:
            db.add_event("user", "import", True, "Imported a script with risky calls after confirmation:\n"
                         + "\n".join(f"line {f['line']}: {f['message']}" for f in check["findings"]))
        return result

    @app.post("/api/recipes/{recipe_id}/start")
    async def start(recipe_id: int, force: bool = False):
        return {"message": await manager.start(recipe_id, force=force)}

    @app.post("/api/recipes/{recipe_id}/stop")
    async def stop(recipe_id: int):
        return {"message": await manager.stop(recipe_id)}

    @app.post("/api/recipes/{recipe_id}/restart")
    async def restart(recipe_id: int, force: bool = False):
        return {"message": await manager.restart(recipe_id, force=force)}

    @app.post("/api/recipes/{recipe_id}/switch")
    async def switch(recipe_id: int):
        return {"message": await manager.switch(recipe_id)}

    @app.get("/api/recipes/{recipe_id}/logs", response_class=PlainTextResponse)
    async def logs(recipe_id: int, tail: int = 500):
        r = get_recipe_or_404(recipe_id)
        return await manager.logs(r, tail=max(1, min(tail, 20000)))

    @app.get("/api/recipes/{recipe_id}/logs/stream")
    async def logs_stream(recipe_id: int, tail: int = 200):
        r = get_recipe_or_404(recipe_id)

        async def events():
            async for line in manager.follow_logs(r, tail=max(0, min(tail, 20000))):
                yield f"data: {line}\n\n"
            yield "event: end\ndata: \n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- schedules -----------------------------------------------------

    def check_recipe_ref(body: ScheduleIn) -> None:
        if body.recipe_id is not None:
            get_recipe_or_404(body.recipe_id)
        if body.host_id is not None:
            get_host_or_404(body.host_id)

    def next_run_for(body: ScheduleIn) -> Optional[str]:
        return compute_next_run(body.model_dump()) if body.enabled else None

    @app.get("/api/schedules")
    async def list_schedules():
        return db.list_schedules()

    @app.get("/api/schedules/timeline")
    async def schedule_timeline(hours: int = 168):
        """Which model runs when over the next `hours`, if every scheduled action succeeds."""
        start = local_now().replace(second=0, microsecond=0)
        end = start + timedelta(hours=max(1, min(hours, 24 * 31)))
        recipes = await manager.recipes_with_status()
        running = {r["id"] for r in recipes if r["status"] in ("ready", "loading", "starting")}
        hosts = {h["id"]: h["name"] for h in db.list_hosts()}
        return build_timeline(db.list_schedules(), recipes, running, start, end, hosts)

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(body: ScheduleIn):
        check_recipe_ref(body)
        data = body.model_dump()
        data["enabled"] = int(body.enabled)
        sid = db.create_schedule(data, next_run_for(body))
        return db.get_schedule(sid)

    @app.put("/api/schedules/{schedule_id}")
    async def update_schedule(schedule_id: int, body: ScheduleIn):
        if not db.get_schedule(schedule_id):
            raise HTTPException(404, "Schedule not found")
        check_recipe_ref(body)
        data = body.model_dump()
        data["enabled"] = int(body.enabled)
        db.update_schedule(schedule_id, data, next_run_for(body))
        return db.get_schedule(schedule_id)

    @app.delete("/api/schedules/{schedule_id}", status_code=204)
    async def delete_schedule(schedule_id: int):
        db.delete_schedule(schedule_id)
        return Response(status_code=204)

    @app.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: int):
        s = db.get_schedule(schedule_id)
        if not s:
            raise HTTPException(404, "Schedule not found")
        ok, msg = await scheduler.run_schedule(s, source=f"manual:{s['name']}")
        if not ok:
            raise HTTPException(409, msg)
        return {"message": msg}

    # ---- activity ------------------------------------------------------

    @app.get("/api/events")
    async def events(limit: int = 200):
        return db.list_events(max(1, min(limit, 2000)))

    # ---- UI ------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    if config.MODE == "agent":
        if not config.AGENT_TOKEN:
            raise SystemExit("DGXM_MODE=agent needs DGXM_AGENT_TOKEN (a long random secret shared with the hub)")
        uvicorn.run(create_agent_app(), host=config.HOST, port=config.PORT, log_level="info")
        return
    if not config.AUTH_PASSWORD:
        logging.warning("DGXM_PASSWORD is not set - the dashboard is open to anyone who can reach port %s", config.PORT)
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()
