"""Cron / one-shot scheduler that fires recipe actions."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from croniter import croniter

from . import config
from .db import Database
from .manager import ActionError, Manager

log = logging.getLogger("dgxspark_manager.scheduler")

# Actions a schedule can run. `needs_recipe` drives validation and the UI.
ACTIONS = {
    "start": {"label": "Start model", "needs_recipe": True},
    "stop": {"label": "Stop model", "needs_recipe": True},
    "restart": {"label": "Restart model", "needs_recipe": True},
    "switch": {"label": "Switch to model (stop others on its host, free memory, start)", "needs_recipe": True},
    "stop_all": {"label": "Stop all models", "needs_recipe": False},
    "free_memory": {"label": "Stop all models and free memory", "needs_recipe": False},
}


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        # Times entered in the browser without an offset are Spark-local.
        dt = dt.astimezone()
    return dt


def validate_cron(expr: str) -> None:
    if not croniter.is_valid(expr):
        raise ValueError(f"Invalid cron expression: {expr!r}")


def compute_next_run(schedule: dict, after: Optional[datetime] = None) -> Optional[str]:
    after = after or local_now()
    if schedule.get("cron"):
        return croniter(schedule["cron"], after).get_next(datetime).isoformat(timespec="seconds")
    if schedule.get("run_at"):
        return parse_time(schedule["run_at"]).isoformat(timespec="seconds")
    return None


async def execute(manager: Manager, schedule: dict, source: str) -> str:
    action = schedule["action"]
    rid = schedule.get("recipe_id")
    if action == "start":
        return await manager.start(rid, source)
    if action == "stop":
        return await manager.stop(rid, source)
    if action == "restart":
        return await manager.restart(rid, source)
    if action == "switch":
        return await manager.switch(rid, source)
    if action == "stop_all":
        return await manager.stop_all(source, host_id=schedule.get("host_id"))
    if action == "free_memory":
        return await manager.free_memory(source, host_id=schedule.get("host_id"))
    raise ActionError(f"Unknown action {action!r}")


class Scheduler:
    def __init__(self, db: Database, manager: Manager):
        self.db = db
        self.manager = manager
        self._task: Optional[asyncio.Task] = None

    def resync(self) -> None:
        """On startup: recompute cron schedules from now (missed runs are skipped) and
        expire one-shot schedules that were missed by more than the grace period."""
        now = local_now()
        for s in self.db.list_schedules():
            if not s["enabled"]:
                continue
            if s["cron"]:
                self.db.set_schedule_next_run(s["id"], compute_next_run(s, now), True)
            elif s["next_run"] and parse_time(s["next_run"]) < now - timedelta(seconds=config.ONCE_GRACE_SECONDS):
                self.db.set_schedule_next_run(s["id"], None, False)
                self.db.record_schedule_run(s["id"], False, "Missed while the manager was not running")

    async def run_schedule(self, schedule: dict, source: str) -> tuple[bool, str]:
        try:
            msg = await execute(self.manager, schedule, source)
            ok = True
        except ActionError as e:
            ok, msg = False, e.message
        except Exception as e:  # keep the loop alive no matter what
            log.exception("schedule %s failed", schedule["id"])
            ok, msg = False, f"{type(e).__name__}: {e}"
        self.db.record_schedule_run(schedule["id"], ok, msg)
        return ok, msg

    async def tick(self) -> None:
        now = local_now()
        for s in self.db.list_schedules():
            if not s["enabled"] or not s["next_run"] or parse_time(s["next_run"]) > now:
                continue
            # Advance first so a slow action can't fire twice.
            if s["cron"]:
                self.db.set_schedule_next_run(s["id"], compute_next_run(s, now), True)
            else:
                self.db.set_schedule_next_run(s["id"], None, False)
            await self.run_schedule(s, source=f"schedule:{s['name']}")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(config.SCHEDULER_TICK)

    def start(self) -> None:
        self.resync()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
