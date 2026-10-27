"""Agent mode (DGXM_MODE=agent): no dashboard, no database, no scheduler.

The hub sends complete recipes to /agent/v1/... and the agent runs them on this machine with the
LocalExecutor (Docker containers or native processes). Every request needs
`Authorization: Bearer <DGXM_AGENT_TOKEN>`; the token grants full control over models on this host.
"""

import secrets
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, config
from .executor import ActionError, LocalExecutor, host_info
from .selfmetrics import Sink


class AgentRequest(BaseModel):
    recipe: dict[str, Any] = {}
    recipes: list[dict[str, Any]] = []
    tail: int = 500


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


def create_agent_app(executor: Optional[LocalExecutor] = None) -> FastAPI:
    ex = executor or LocalExecutor()
    app = FastAPI(title="Local LLM Manager agent", version=__version__)
    auth = [Depends(require_token)]
    metrics = Sink()
    app.state.metrics = metrics

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
        return (f"<!doctype html><title>Local LLM Manager agent</title><body style='font-family:sans-serif'>"
                f"<h3>Local LLM Manager agent on {host_info()['hostname']}</h3>"
                f"<p>This machine is controlled from a hub dashboard. Add it there under <b>Hosts</b> "
                f"with this URL and its agent token.</p></body>")

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

    @app.post("/agent/v1/drop-caches", dependencies=auth)
    async def drop_caches():
        ok, message = await ex.drop_caches()
        return {"ok": ok, "message": message}

    return app
