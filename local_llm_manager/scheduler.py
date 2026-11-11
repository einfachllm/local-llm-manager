"""Cron / one-shot scheduler that fires recipe actions."""

import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from . import config
from .db import Database
from .manager import ActionError, Manager

log = logging.getLogger("local_llm_manager.scheduler")

# Actions a schedule can run. `needs_recipe` drives validation and the UI.
ACTIONS = {
    "start": {"label": "Start model", "needs_recipe": True},
    "stop": {"label": "Stop model", "needs_recipe": True},
    "restart": {"label": "Restart model", "needs_recipe": True},
    "switch": {"label": "Switch to model (stop others on its host, free memory, start)", "needs_recipe": True},
    "stop_all": {"label": "Stop all models", "needs_recipe": False},
    "free_memory": {"label": "Stop all models and free memory", "needs_recipe": False},
}


def local_zone() -> Optional[ZoneInfo]:
    """The hub's IANA zone, so croniter sees DST transitions. A fixed-offset tzinfo
    (datetime.now().astimezone()) carries no transition rules: croniter would keep the old
    offset across the change and fire schedules an hour off. Resolution: TZ, then the
    /etc/localtime symlink, then /etc/timezone (Debian/Ubuntu; in Docker /etc/localtime is
    often a file bind-mount, not a symlink). None when nothing resolves - the caller falls
    back to the fixed offset, which is correct for UTC (no DST)."""
    tz = os.environ.get("TZ", "").strip().lstrip(":")
    if tz:
        try:
            return ZoneInfo(tz)
        except (ValueError, KeyError):
            pass
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        target = ""
    _, sep, key = target.partition("zoneinfo/")
    if sep and key:
        try:
            return ZoneInfo(key)
        except (ValueError, KeyError):
            pass
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return ZoneInfo(key)
    except (OSError, ValueError, KeyError):
        pass
    return None


def local_now() -> datetime:
    zone = local_zone()
    return datetime.now(zone) if zone else datetime.now().astimezone()


def _elapsed(a: datetime, b: datetime) -> float:
    """Seconds from a to b on the real time line. Plain subtraction of two aware datetimes that share
    a tzinfo runs in WALL time (PEP 495), so a DST fall-back's repeated hour would measure 0 s."""
    return (a.astimezone(timezone.utc) - b.astimezone(timezone.utc)).total_seconds()


def is_second_instance(t: datetime) -> bool:
    """True when t is the second occurrence of a wall time that a DST fall-back repeats
    (same date and clock, smaller offset). Only a real zone knows both folds; a fixed-offset
    datetime never repeats, and spring-forward's non-existent times are not ambiguous."""
    if t.tzinfo is None:
        return False
    first, second = t.replace(fold=0).utcoffset(), t.replace(fold=1).utcoffset()
    return first != second and t.utcoffset() == second


def parse_time(value: str) -> datetime:
    if value.endswith("Z"):
        # Python 3.10's fromisoformat does not know the "Z" suffix (3.11+ does).
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        # Times entered in the browser without an offset are Spark-local.
        dt = dt.astimezone()
    return dt


def validate_cron(expr: str) -> None:
    if not croniter.is_valid(expr):
        raise ValueError(f"Invalid cron expression: {expr!r}")


def compute_next_run(schedule: dict, after: Optional[datetime] = None,
                     fired: Optional[datetime] = None) -> Optional[str]:
    """`fired` is the occurrence that just ran (tick): the reference for the DST skip, so a
    late tick still recognizes the repetition. Without it, `after` is the reference. The result
    is always written in the hub's zone (same instant), so UI and API see one consistent wall
    clock no matter which offset a one-shot was submitted with."""
    after = after or local_now()
    zone = local_zone()
    if schedule.get("cron"):
        it = croniter(schedule["cron"], after)
        nxt = it.get_next(datetime)
        # A DST fall-back repeats a wall time and croniter hands out both instances. Skip the
        # second only when it breaks the schedule's own rhythm: a daily 02:30 must not fire
        # twice an hour apart, but an hourly cron's second 02:00 is its next regular slot (one
        # real hour later) and fires. Spring-forward needs no skip: croniter moves a
        # non-existent time to the transition (02:30 -> 03:00), which is unambiguous.
        if is_second_instance(nxt):
            following = it.get_next(datetime)
            ref = fired or after
            if _elapsed(nxt, ref) < _elapsed(following, nxt):
                nxt = following
        return nxt.astimezone(zone).isoformat(timespec="seconds")
    if schedule.get("run_at"):
        return parse_time(schedule["run_at"]).astimezone(zone).isoformat(timespec="seconds")
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
        # Self-metrics: how many scheduled runs succeeded / failed since the manager started.
        self.runs: Counter = Counter()

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
        self.runs["ok" if ok else "error"] += 1
        return ok, msg

    async def tick(self) -> None:
        if self.db.get_setting("schedules_paused") != "1":
            now = local_now()
            for s in self.db.list_schedules():
                if not s["enabled"] or not s["next_run"] or parse_time(s["next_run"]) > now:
                    continue
                # Advance first so a slow action can't fire twice. `fired` anchors the DST
                # skip to the occurrence that ran, so a late tick still sees the repetition.
                if s["cron"]:
                    self.db.set_schedule_next_run(
                        s["id"], compute_next_run(s, now, fired=parse_time(s["next_run"])), True)
                else:
                    self.db.set_schedule_next_run(s["id"], None, False)
                await self.run_schedule(s, source=f"schedule:{s['name']}")
        # Close start-time measurements even when no browser has the dashboard open.
        if self.db.pending_starts():
            recipes = [r for rid in self.db.pending_starts() if (r := self.db.get_recipe(rid))]
            if recipes:
                self.manager._check_start_times(await self.manager._statuses(recipes))

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
