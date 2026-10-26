"""Project enabled schedules onto a time window: when will each model be running?

This is a plan, not a prediction of success: it assumes every action works (enough memory,
image present, ...). Starting point is the set of models running right now.
"""

from datetime import datetime
from typing import Any, Iterable, Optional

from croniter import croniter

from .scheduler import parse_time

MAX_EVENTS_PER_SCHEDULE = 500
GLOBAL_ACTIONS = ("stop_all", "free_memory")


def fire_times(schedule: dict, start: datetime, end: datetime) -> list[datetime]:
    if schedule.get("cron"):
        it = croniter(schedule["cron"], start)
        times = []
        while len(times) < MAX_EVENTS_PER_SCHEDULE:
            t = it.get_next(datetime)
            if t > end:
                break
            times.append(t)
        return times
    if schedule.get("next_run"):
        t = parse_time(schedule["next_run"])
        if start <= t <= end:
            return [t]
    return []


def build_timeline(schedules: Iterable[dict], recipes: Iterable[dict], running_ids: set[int],
                   start: datetime, end: datetime, hosts: Optional[dict[int, str]] = None) -> dict[str, Any]:
    """`hosts` maps host id -> name. "switch" only stops models on the same host; stop_all /
    free_memory stop the models of their host (or of all hosts when the schedule has none)."""
    recipes = {r["id"]: r for r in recipes}
    hosts = hosts or {}
    host_of = lambda rid: recipes[rid].get("host_id")
    events = []
    for s in schedules:
        if not s["enabled"]:
            continue
        if s["recipe_id"] is not None and s["recipe_id"] not in recipes:
            continue
        for t in fire_times(s, start, end):
            events.append({"time": t, "schedule_id": s["id"], "schedule": s["name"],
                           "action": s["action"], "recipe_id": s["recipe_id"], "host_id": s.get("host_id")})
    events.sort(key=lambda e: (e["time"], e["schedule_id"]))

    blocks: dict[int, list[dict]] = {rid: [] for rid in recipes}
    open_blocks: dict[int, dict] = {
        rid: {"start": start, "started_by": None, "started_by_id": None, "running_at_start": True}
        for rid in running_ids if rid in recipes
    }

    def close(rid: int, t: datetime, event: dict) -> None:
        b = open_blocks.pop(rid, None)
        if b:
            blocks[rid].append({**b, "end": t, "stopped_by": event["schedule"],
                                "stopped_by_id": event["schedule_id"], "open_end": False})

    def open_(rid: int, t: datetime, event: dict) -> None:
        if rid not in open_blocks:
            open_blocks[rid] = {"start": t, "started_by": event["schedule"],
                                "started_by_id": event["schedule_id"], "running_at_start": False}

    for e in events:
        rid, action, t = e["recipe_id"], e["action"], e["time"]
        if action in ("start", "restart"):
            open_(rid, t, e)
        elif action == "stop":
            close(rid, t, e)
        elif action == "switch":
            for other in list(open_blocks):
                if other != rid and host_of(other) == host_of(rid):
                    close(other, t, e)
            open_(rid, t, e)
        elif action in GLOBAL_ACTIONS:
            for other in list(open_blocks):
                if e["host_id"] is None or host_of(other) == e["host_id"]:
                    close(other, t, e)
    for rid, b in list(open_blocks.items()):
        blocks[rid].append({**b, "end": end, "stopped_by": None, "stopped_by_id": None, "open_end": True})

    # One lane per model that runs in the window or is targeted by a schedule.
    targeted = {e["recipe_id"] for e in events if e["recipe_id"] is not None}
    targeted |= {s["recipe_id"] for s in schedules if s["enabled"] and s["recipe_id"] in recipes}
    lane_ids = [rid for rid in recipes if blocks[rid] or rid in targeted]
    host_name = lambda rid: hosts.get(host_of(rid), "")
    lane_ids.sort(key=lambda rid: (host_name(rid).lower(), recipes[rid]["name"].lower()))

    iso = lambda d: d.isoformat(timespec="seconds")
    return {
        "start": iso(start),
        "end": iso(end),
        "lanes": [
            {
                "recipe_id": rid,
                "name": recipes[rid]["name"],
                "host_id": host_of(rid),
                "host": host_name(rid),
                "color": recipes[rid].get("color", ""),
                "blocks": [{**b, "start": iso(b["start"]), "end": iso(b["end"])}
                           for b in sorted(blocks[rid], key=lambda b: b["start"])],
            }
            for rid in lane_ids
        ],
        "events": [{**e, "time": iso(e["time"])} for e in events],
    }
