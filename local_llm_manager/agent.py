"""Agent mode (DGXM_MODE=agent): no dashboard, no database, no scheduler.

The hub sends complete recipes to /agent/v1/... and the agent runs them on this machine with the
LocalExecutor (Docker containers or native processes). Every request needs
`Authorization: Bearer <DGXM_AGENT_TOKEN>`; the token grants full control over models on this host.
"""

import asyncio
import html
import secrets
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, config
from .executor import ActionError, LocalExecutor, host_info
from .joining import Joiner
from .selfmetrics import Sink
from .shares import add_share_routes


class AgentRequest(BaseModel):
    recipe: dict[str, Any] = {}
    recipes: list[dict[str, Any]] = []
    items: list[dict[str, str]] = []
    tail: int = 500


class TransferRequest(BaseModel):
    kind: Literal["pull", "download", "copy"]
    target: str = Field(min_length=1, max_length=300)   # image reference, Hugging Face model id, or copy source
    name: str = Field(default="", max_length=128)       # download / copy: folder name in the model dir
    share: dict[str, str] = {}                          # copy: {"id", "ticket"} from the source's /shares


class ShareRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


def require_token(request: Request) -> None:
    if not config.AGENT_TOKEN:
        raise HTTPException(403, "Agent API is disabled: set DGXM_AGENT_TOKEN on this machine")
    header = request.headers.get("authorization", "")
    given = header[7:] if header.lower().startswith("bearer ") else ""
    if not secrets.compare_digest(given.encode(), config.AGENT_TOKEN.encode()):
        raise HTTPException(401, "Invalid agent token")


def _recipe(body: AgentRequest) -> dict:
    if not body.recipe.get("container_name"):
        raise HTTPException(422, "recipe.container_name is required")
    return body.recipe


def create_agent_app(executor: Optional[LocalExecutor] = None,
                     join_transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    ex = executor or LocalExecutor()
    # DGXM_HUB_URL: ask that hub to add this machine (approved there by comparing the join code).
    joiner = Joiner(config.HUB_URL, config.AGENT_TOKEN, host_info(), join_transport) \
        if config.HUB_URL and config.AGENT_TOKEN else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(joiner.run()) if joiner else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Local LLM Manager agent", version=__version__, lifespan=lifespan)
    app.state.joiner = joiner
    auth = [Depends(require_token)]
    metrics = Sink()
    app.state.metrics, app.state.executor = metrics, ex

    @app.exception_handler(ActionError)
    async def action_error(_: Request, exc: ActionError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        response = await call_next(request)
        route = request.scope.get("route")
        if route is not None and getattr(route, "path", None) and request.url.path not in ("/healthz", "/metrics"):
            metrics.add("dgxm_api_requests_total", "Agent API requests, by method, route and status.",
                        method=request.method, path=route.path, status=str(response.status_code))
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        join = ""
        if joiner:
            st = joiner.state
            what = {"pending": "waiting for approval", "approved": "added ✓", "rejected": "rejected",
                    "error": f"cannot reach the hub: {st['message']}"}.get(st["status"], st["status"])
            join = (f"<p>Hub <code>{html.escape(st['hub'])}</code>: <b>{html.escape(what)}</b><br>"
                    f"Join code: <b style='font-size:1.6em;letter-spacing:2px'>{st['code']}</b><br>"
                    f"Approve it under <b>Hosts</b> on the hub only if the hub shows the same code.</p>")
        return (f"<!doctype html><title>Local LLM Manager agent</title><body style='font-family:sans-serif'>"
                f"<h3>Local LLM Manager agent on {html.escape(host_info()['hostname'])}</h3>"
                f"<p>This machine is controlled from a hub dashboard. Add it there under <b>Hosts</b> "
                f"with this URL and its agent token, or set <code>DGXM_HUB_URL</code> to ask the hub to add it.</p>"
                f"{join}</body>")

    # Liveness / self-metrics for external scrapers: no token needed, no sensitive data.
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/metrics", include_in_schema=False)
    async def self_metrics():
        if not config.METRICS_ENABLED:
            raise HTTPException(404)
        metrics.gauge("dgxm_build_info", "Manager build.", 1.0, version=__version__, mode="agent")
        metrics.gauge("dgxm_uptime_seconds", "Seconds since the agent started.", metrics.uptime())
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/agent/v1/info", dependencies=auth)
    async def info():
        return await ex.info()

    @app.get("/agent/v1/system", dependencies=auth)
    async def system():
        return await ex.system()

    @app.post("/agent/v1/statuses", dependencies=auth)
    async def statuses(body: AgentRequest):
        return await ex.statuses(body.recipes)

    @app.post("/agent/v1/start", dependencies=auth)
    async def start(body: AgentRequest):
        return {"message": await ex.start(_recipe(body))}

    @app.post("/agent/v1/stop", dependencies=auth)
    async def stop(body: AgentRequest):
        return {"message": await ex.stop(_recipe(body))}

    @app.post("/agent/v1/logs", dependencies=auth, response_class=PlainTextResponse)
    async def logs(body: AgentRequest):
        return await ex.logs(_recipe(body), max(1, min(body.tail, 20000)))

    @app.post("/agent/v1/logs/stream", dependencies=auth)
    async def logs_stream(body: AgentRequest):
        recipe = _recipe(body)

        async def lines():
            async for line in ex.follow_logs(recipe, max(0, min(body.tail, 20000))):
                yield line + "\n"

        return StreamingResponse(lines(), media_type="text/plain; charset=utf-8",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/agent/v1/images", dependencies=auth)
    async def images():
        return await ex.images()

    @app.post("/agent/v1/models/check", dependencies=auth)
    async def models_check(body: AgentRequest):
        return await ex.check_models(body.items)

    @app.get("/agent/v1/weights/scan", dependencies=auth)
    async def weights_scan():
        return await ex.scan_weights()

    @app.post("/agent/v1/transfers", dependencies=auth)
    async def start_transfer(body: TransferRequest):
        """Pull an image or download weights in the background; GET the returned id for its progress."""
        return await ex.start_transfer(body.kind, body.target, body.name, body.share)

    @app.post("/agent/v1/shares", dependencies=auth)
    async def create_share(body: ShareRequest):
        """Share a path read-only so another machine can copy it (see shares.py)."""
        return await ex.create_share(body.path)

    # Reading a share needs its ticket, not the agent token.
    add_share_routes(app, ex.shares)

    @app.get("/agent/v1/transfers/{task_id}", dependencies=auth)
    async def transfer(task_id: str):
        return await ex.transfer(task_id)

    @app.post("/agent/v1/drop-caches", dependencies=auth)
    async def drop_caches():
        ok, message = await ex.drop_caches()
        return {"ok": ok, "message": message}

    return app
