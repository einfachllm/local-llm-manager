from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import recipe_body

from local_llm_manager.scheduler import local_now
from local_llm_manager.timeline import build_timeline, fire_times

BERLIN = ZoneInfo("Europe/Berlin")

T0 = local_now().replace(year=2030, month=1, day=7, hour=0, minute=0, second=0, microsecond=0)  # a Monday


def sched(id, action, recipe_id=None, cron=None, next_run=None, enabled=1):
    return {"id": id, "name": f"s{id}", "action": action, "recipe_id": recipe_id, "cron": cron,
            "next_run": next_run, "enabled": enabled}


RECIPES = [{"id": 1, "name": "A", "color": "green"}, {"id": 2, "name": "B", "color": ""},
           {"id": 3, "name": "C", "color": ""}]


def blocks(tl, rid):
    lane = next((l for l in tl["lanes"] if l["recipe_id"] == rid), None)
    return [(b["start"][11:16], b["end"][11:16], b["open_end"]) for b in lane["blocks"]] if lane else None


def test_start_stop_and_switch():
    schedules = [
        sched(1, "start", 1, cron="0 8 * * *"),
        sched(2, "stop", 1, cron="0 18 * * *"),
        sched(3, "switch", 2, cron="0 22 * * *"),
        sched(4, "free_memory", cron="0 6 * * *"),
        sched(5, "start", 3, cron="0 1 * * *", enabled=0),
    ]
    tl = build_timeline(schedules, RECIPES, running_ids={3}, start=T0, end=T0 + timedelta(hours=24))
    assert blocks(tl, 1) == [("08:00", "18:00", False)]
    assert blocks(tl, 2) == [("22:00", "00:00", True)]   # still running at window end
    assert blocks(tl, 3) == [("00:00", "06:00", False)]  # running now, stopped by free_memory
    assert [l["name"] for l in tl["lanes"]] == ["A", "B", "C"]
    assert tl["lanes"][0]["color"] == "green"
    assert [e["time"][11:16] for e in tl["events"]] == ["06:00", "08:00", "18:00", "22:00"]


def test_switch_closes_other_models_and_once_schedules():
    schedules = [
        sched(1, "start", 1, next_run=(T0 + timedelta(hours=2)).isoformat()),
        sched(2, "switch", 2, next_run=(T0 + timedelta(hours=5)).isoformat()),
        sched(3, "start", 3, next_run=(T0 + timedelta(days=3)).isoformat()),  # outside window
    ]
    tl = build_timeline(schedules, RECIPES, set(), T0, T0 + timedelta(hours=12))
    assert blocks(tl, 1) == [("02:00", "05:00", False)]
    assert blocks(tl, 2) == [("05:00", "12:00", True)]
    assert blocks(tl, 3) == []  # has a schedule, so it gets an (empty) lane


def test_timeline_endpoint(client):
    r = client.post("/api/recipes", json=recipe_body()).json()
    client.post("/api/schedules", json={"name": "go", "action": "start", "recipe_id": r["id"], "cron": "0 * * * *"})
    tl = client.get("/api/schedules/timeline?hours=24").json()
    assert len(tl["events"]) == 24
    assert tl["lanes"][0]["name"] == r["name"] and len(tl["lanes"][0]["blocks"]) == 1


def test_switch_and_free_memory_only_affect_their_host():
    recipes = [{"id": 1, "name": "spark-a", "host_id": 1}, {"id": 2, "name": "spark-b", "host_id": 1},
               {"id": 3, "name": "mac-a", "host_id": 2}]
    schedules = [
        sched(1, "switch", 2, cron="0 2 * * *"),
        {**sched(2, "free_memory", cron="0 3 * * *"), "host_id": 2},
    ]
    tl = build_timeline(schedules, recipes, running_ids={1, 3}, start=T0, end=T0 + timedelta(hours=6),
                        hosts={1: "spark", 2: "mac"})
    assert blocks(tl, 1) == [("00:00", "02:00", False)]   # switched away on the same host
    assert blocks(tl, 2) == [("02:00", "06:00", True)]
    assert blocks(tl, 3) == [("00:00", "03:00", False)]   # only mac's free_memory stops it
    assert [(l["host"], l["name"]) for l in tl["lanes"]] == [("mac", "mac-a"), ("spark", "spark-a"), ("spark", "spark-b")]


# ---- TIME-02: the plan matches the scheduler's DST rhythm ---------------------


def test_fire_times_fall_back_daily_once():
    s = {"cron": "30 2 * * *"}
    times = fire_times(s, datetime(2026, 10, 24, 0, 0, tzinfo=BERLIN),
                       datetime(2026, 10, 27, 0, 0, tzinfo=BERLIN))
    # One 02:30 per day; on the fall-back day only the first instance (+02:00) runs - the repeated
    # wall time is skipped because it would fire again one real hour later.
    assert times == [datetime(2026, 10, 24, 2, 30, tzinfo=BERLIN),
                     datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN),
                     datetime(2026, 10, 26, 2, 30, tzinfo=BERLIN)]


def test_fire_times_fall_back_hourly_keeps_both_slots():
    s = {"cron": "0 * * * *"}
    times = fire_times(s, datetime(2026, 10, 25, 0, 30, tzinfo=BERLIN),
                       datetime(2026, 10, 25, 4, 30, tzinfo=BERLIN))
    assert times == [datetime(2026, 10, 25, 1, 0, tzinfo=BERLIN),
                     datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN),
                     datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN, fold=1),
                     datetime(2026, 10, 25, 3, 0, tzinfo=BERLIN),
                     datetime(2026, 10, 25, 4, 0, tzinfo=BERLIN)]
    assert [t.utcoffset() for t in times] == [timedelta(hours=2)] * 2 + [timedelta(hours=1)] * 3


def test_fire_times_spring_transition_once():
    s = {"cron": "30 2 * * *"}
    times = fire_times(s, datetime(2026, 3, 28, 0, 0, tzinfo=BERLIN),
                       datetime(2026, 3, 31, 0, 0, tzinfo=BERLIN))
    # 02:30 does not exist on the spring-forward day: croniter moves it to the transition (03:00),
    # so the plan shows one run, not a gap.
    assert times == [datetime(2026, 3, 28, 2, 30, tzinfo=BERLIN),
                     datetime(2026, 3, 29, 3, 0, tzinfo=BERLIN),
                     datetime(2026, 3, 30, 2, 30, tzinfo=BERLIN)]


# ---- TIME-04: switch is kind-aware (agents are kept, switch-to-agent refused) --


def test_switch_keeps_agents_running():
    recipes = [{"id": 1, "name": "model", "host_id": 1, "kind": "model"},
               {"id": 2, "name": "agent", "host_id": 1, "kind": "agent"}]
    schedules = [sched(1, "switch", 1, cron="0 2 * * *")]
    tl = build_timeline(schedules, recipes, running_ids={2}, start=T0, end=T0 + timedelta(hours=6),
                        hosts={1: "spark"})
    assert blocks(tl, 2) == [("00:00", "06:00", True)]   # agent keeps running across the switch
    assert blocks(tl, 1) == [("02:00", "06:00", True)]   # the model starts


def test_switch_to_agent_is_not_planned_as_run():
    recipes = [{"id": 1, "name": "model", "host_id": 1, "kind": "model"},
               {"id": 2, "name": "agent", "host_id": 1, "kind": "agent"}]
    schedules = [sched(1, "switch", 2, cron="0 2 * * *")]
    tl = build_timeline(schedules, recipes, running_ids={1}, start=T0, end=T0 + timedelta(hours=6),
                        hosts={1: "spark"})
    assert blocks(tl, 1) == [("00:00", "06:00", True)]   # refused switch stops nothing
    assert blocks(tl, 2) == []                            # no run block for the agent
    ev = next(e for e in tl["events"] if e["action"] == "switch")
    assert "agent" in ev["error"]


def test_stop_all_still_stops_agents():
    recipes = [{"id": 1, "name": "model", "host_id": 1, "kind": "model"},
               {"id": 2, "name": "agent", "host_id": 1, "kind": "agent"}]
    schedules = [sched(1, "stop_all", cron="0 2 * * *")]
    tl = build_timeline(schedules, recipes, running_ids={1, 2}, start=T0, end=T0 + timedelta(hours=6),
                        hosts={1: "spark"})
    assert blocks(tl, 1) == [("00:00", "02:00", False)]
    assert blocks(tl, 2) == [("00:00", "02:00", False)]   # stop_all does not keep agents
