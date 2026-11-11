"""Schedule time contract (B-09 / TIME-01): an explicit offset keeps its instant, a naive time
is hub-local, and re-saving a schedule never shifts it. Plus: the "Z" suffix must work on
Python 3.10 (TIME-09), whose fromisoformat does not accept it.

All expectations are derived from absolute instants (UTC arithmetic), so these tests make no
assumption about the hub's time zone and none about DST.
"""

from datetime import datetime, timedelta, timezone

from conftest import recipe_body

from local_llm_manager import scheduler as sched
from local_llm_manager.scheduler import compute_next_run, parse_time

# A zone that is (almost) certainly not the hub's, to prove offsets are honoured, not stripped.
FOREIGN = timezone(timedelta(hours=5, minutes=30))


def future_instant(hours=2):
    """A whole-second instant some hours ahead, as an aware UTC datetime."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0)


def create_once(client, run_at, name="once"):
    r = client.post("/api/recipes", json=recipe_body()).json()
    body = {"name": name, "action": "start", "recipe_id": r["id"], "run_at": run_at}
    s = client.post("/api/schedules", json=body)
    assert s.status_code == 201, s.text
    return r, s.json()


# ---- parse_time / compute_next_run -------------------------------------------


def test_parse_time_naive_is_hub_local():
    dt = parse_time("2030-06-15T12:30")
    assert dt.tzinfo is not None
    # The wall clock is kept and read in the hub's zone, whatever its offset is.
    assert dt == datetime(2030, 6, 15, 12, 30).astimezone()
    assert dt.replace(tzinfo=None) == datetime(2030, 6, 15, 12, 30)


def test_parse_time_keeps_explicit_instant():
    cases = [
        ("2030-06-15T12:30:00+00:00", timezone.utc),
        ("2030-06-15T12:30:00+05:30", FOREIGN),
        ("2030-06-15T12:30:00-07:00", timezone(timedelta(hours=-7))),
    ]
    for value, zone in cases:
        assert parse_time(value) == datetime(2030, 6, 15, 12, 30, tzinfo=zone)


def test_parse_time_z_suffix():
    assert parse_time("2030-06-15T12:30:00Z") == datetime(2030, 6, 15, 12, 30, tzinfo=timezone.utc)


def test_parse_time_z_suffix_without_python311(monkeypatch):
    """Regression (TIME-09): the project supports Python 3.10, whose fromisoformat rejects 'Z'.
    Simulate that parser so the test fails on any Python when parse_time stops normalizing."""

    class Py310DateTime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            if value.endswith("Z"):
                raise ValueError("fromisoformat before Python 3.11 does not accept 'Z'")
            return super().fromisoformat(value)

    monkeypatch.setattr(sched, "datetime", Py310DateTime)
    assert sched.parse_time("2030-06-15T12:30:00Z") == datetime(2030, 6, 15, 12, 30, tzinfo=timezone.utc)


def test_compute_next_run_once_aware():
    t = future_instant()
    # Submitted in UTC: next_run is the same instant ...
    assert datetime.fromisoformat(compute_next_run({"run_at": t.isoformat()})) == t
    # ... and submitted in the hub's zone it comes back unchanged (the UI's roundtrip).
    hub = t.astimezone()
    assert compute_next_run({"run_at": hub.isoformat()}) == hub.isoformat(timespec="seconds")
    # Naive input keeps the wall clock in the hub's zone.
    naive = datetime(2030, 6, 15, 12, 30)
    assert datetime.fromisoformat(compute_next_run({"run_at": naive.isoformat()})) == naive.astimezone()


# ---- through the API ----------------------------------------------------------


def test_create_schedule_with_offset_keeps_instant(client):
    t = future_instant()
    _, s = create_once(client, t.isoformat())  # "+00:00" suffix
    assert datetime.fromisoformat(s["next_run"]) == t
    # Same hub wall clock as the submitted instant, whatever offset next_run is written in.
    assert datetime.fromisoformat(s["next_run"]).astimezone() == t.astimezone()
    assert s["run_at"] == t.isoformat()


def test_create_schedule_with_foreign_offset_keeps_instant(client):
    t = future_instant()
    _, s = create_once(client, t.astimezone(FOREIGN).isoformat())
    assert datetime.fromisoformat(s["next_run"]) == t


def test_update_roundtrip_does_not_shift(client):
    t = future_instant()
    r, s = create_once(client, t.astimezone(FOREIGN).isoformat())
    body = {"name": s["name"], "action": s["action"], "recipe_id": r["id"],
            "run_at": t.astimezone(FOREIGN).isoformat(), "enabled": True}
    # Re-saving the very same value three times must not move the schedule (TIME-01 drift).
    for _ in range(3):
        s = client.put(f"/api/schedules/{s['id']}", json=body).json()
        assert datetime.fromisoformat(s["next_run"]) == t
    # The fixed UI flow: hub wall clock sent back with the hub's offset.
    body["run_at"] = t.astimezone().isoformat()
    s = client.put(f"/api/schedules/{s['id']}", json=body).json()
    assert datetime.fromisoformat(s["next_run"]) == t


def test_naive_run_at_stays_hub_local(client):
    t = future_instant().astimezone()
    _, s = create_once(client, t.strftime("%Y-%m-%dT%H:%M:%S"))
    assert datetime.fromisoformat(s["next_run"]) == t


def test_z_suffix_accepted_via_api(client):
    t = future_instant()
    _, s = create_once(client, t.isoformat().replace("+00:00", "Z"))
    assert datetime.fromisoformat(s["next_run"]) == t


def test_invalid_run_at_rejected(client):
    r = client.post("/api/recipes", json=recipe_body()).json()
    bad = client.post("/api/schedules", json={"name": "x", "action": "start",
                                              "recipe_id": r["id"], "run_at": "tomorrow"})
    assert bad.status_code == 422


def test_tick_fires_at_the_instant_not_the_wall_clock(client, monkeypatch):
    """A one-shot submitted in a foreign zone fires when its instant is due, not when the
    hub's wall clock shows the submitted time."""
    t = future_instant()
    r, s = create_once(client, t.astimezone(FOREIGN).isoformat())
    scheduler = client.app.state.scheduler

    monkeypatch.setattr(sched, "local_now", lambda: t - timedelta(hours=1))
    client.portal.call(scheduler.tick)
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "stopped"
    assert client.get("/api/schedules").json()[0]["enabled"] == 1

    monkeypatch.setattr(sched, "local_now", lambda: t + timedelta(seconds=5))
    client.portal.call(scheduler.tick)
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"
    fired = client.get("/api/schedules").json()[0]
    assert fired["enabled"] == 0 and fired["last_ok"] == 1
