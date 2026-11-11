"""Schedule time contract (B-09 / TIME-01): an explicit offset keeps its instant, a naive time
is hub-local, and re-saving a schedule never shifts it. Plus: the "Z" suffix must work on
Python 3.10 (TIME-09), whose fromisoformat does not accept it.

All expectations are derived from absolute instants (UTC arithmetic), so these tests make no
assumption about the hub's time zone and none about DST.
"""

import builtins
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import recipe_body

from local_llm_manager import config
from local_llm_manager import scheduler as sched
from local_llm_manager.scheduler import compute_next_run, parse_time

# A zone that is (almost) certainly not the hub's, to prove offsets are honoured, not stripped.
FOREIGN = timezone(timedelta(hours=5, minutes=30))
# Fixed zone for the DST tests: fall-back 2026-10-25 (03:00 CEST -> 02:00 CET),
# spring-forward 2026-03-29 (02:00 CET -> 03:00 CEST).
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def _berlin_hub(monkeypatch):
    """Pin the hub zone so next_run's hub-zone normalization (TIME-03) and the DST wall-clock
    assertions are deterministic on any CI machine. Tests that probe zone resolution manage TZ
    themselves."""
    monkeypatch.setenv("TZ", "Europe/Berlin")


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


def test_next_run_is_written_in_the_hub_zone(client):
    # TIME-03: a one-shot submitted in a foreign offset comes back as hub wall clock (Berlin via
    # the autouse fixture), same instant - so UI and API read one consistent time.
    t = future_instant()
    _, s = create_once(client, t.astimezone(FOREIGN).isoformat())
    assert datetime.fromisoformat(s["next_run"]) == t                      # instant unchanged
    assert s["next_run"] == t.astimezone(BERLIN).isoformat(timespec="seconds")  # hub-zone string
    # Cron schedules are hub-zone too.
    r = client.post("/api/recipes", json=recipe_body(name="Cron model", host_port=8123)).json()
    c = client.post("/api/schedules", json={"name": "c", "action": "start",
                                            "recipe_id": r["id"], "cron": "0 8 * * *"}).json()
    nr = datetime.fromisoformat(c["next_run"])
    assert nr.utcoffset() == nr.astimezone(BERLIN).utcoffset()


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


# ---- TIME-02: DST (fixed zone Europe/Berlin, injected local_now) --------------


def test_local_zone_resolution_chain(monkeypatch):
    real_open = builtins.open
    monkeypatch.delenv("TZ", raising=False)
    # 1) /etc/localtime symlink into the zoneinfo database
    monkeypatch.setattr(sched.os, "readlink", lambda p: "/usr/share/zoneinfo/Europe/Berlin")
    assert sched.local_zone() == BERLIN
    # 2) TZ wins over the symlink
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert sched.local_zone() == BERLIN
    # 3) a POSIX-style TZ is not an IANA key: fall through to the symlink
    monkeypatch.setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")
    assert sched.local_zone() == BERLIN
    # 4) no symlink (Docker file bind-mount): /etc/timezone (Debian/Ubuntu)
    monkeypatch.delenv("TZ")

    def no_link(_):
        raise OSError("not a symlink")

    monkeypatch.setattr(sched.os, "readlink", no_link)

    def etc_timezone(path, *a, **kw):
        if path == "/etc/timezone":
            return io.StringIO("Europe/Berlin\n")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", etc_timezone)
    assert sched.local_zone() == BERLIN
    # 5) nothing resolves: None, and local_now falls back to the fixed offset
    def no_file(path, *a, **kw):
        if path == "/etc/timezone":
            raise FileNotFoundError(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", no_file)
    assert sched.local_zone() is None
    assert isinstance(sched.local_now().tzinfo, timezone)


def test_local_now_uses_the_resolved_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    now = sched.local_now()
    assert isinstance(now.tzinfo, ZoneInfo) and str(now.tzinfo) == "Europe/Berlin"


def test_is_second_instance():
    first = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN)  # fold=0, CEST
    assert not sched.is_second_instance(first)
    assert sched.is_second_instance(first.replace(fold=1))  # CET, the repetition
    assert not sched.is_second_instance(datetime(2026, 10, 25, 3, 0, tzinfo=BERLIN))
    assert not sched.is_second_instance(datetime(2026, 10, 25, 2, 30))  # naive
    fixed = first.astimezone()  # fixed-offset: no transition rules, never a repetition
    assert not sched.is_second_instance(fixed)


def test_dst_daily_fall_back_skips_the_repetition():
    s = {"cron": "30 2 * * *"}
    fired = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN)
    nxt = compute_next_run(s, fired + timedelta(seconds=5), fired=fired)
    assert nxt == "2026-10-26T02:30:00+01:00"


def test_dst_daily_skip_survives_a_late_tick():
    s = {"cron": "30 2 * * *"}
    fired = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN)
    nxt = compute_next_run(s, fired + timedelta(seconds=70), fired=fired)
    assert nxt == "2026-10-26T02:30:00+01:00"


def test_dst_hourly_fires_in_both_hours():
    s = {"cron": "0 * * * *"}
    fired = datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN)
    # The second 02:00 is the next regular hourly slot (one real hour later): it fires.
    assert compute_next_run(s, fired + timedelta(seconds=5), fired=fired) == "2026-10-25T02:00:00+01:00"
    fired2 = fired.replace(fold=1)
    assert compute_next_run(s, fired2 + timedelta(seconds=5), fired=fired2) == "2026-10-25T03:00:00+01:00"


def test_dst_subhourly_keeps_its_second_pass():
    s = {"cron": "*/5 * * * *"}
    fired = datetime(2026, 10, 25, 2, 55, tzinfo=BERLIN)
    assert compute_next_run(s, fired + timedelta(seconds=5), fired=fired) == "2026-10-25T02:00:00+01:00"


def test_dst_spring_moves_to_the_transition_and_fires_once():
    s = {"cron": "30 2 * * *"}
    after = datetime(2026, 3, 29, 1, 30, tzinfo=BERLIN)
    nxt = compute_next_run(s, after)
    assert nxt == "2026-03-29T03:00:00+02:00"  # 02:30 does not exist; croniter moves it
    fired = datetime.fromisoformat(nxt)
    assert compute_next_run(s, fired + timedelta(seconds=5), fired=fired) == "2026-03-30T02:30:00+02:00"


def test_no_dst_normal_day_is_unchanged():
    s = {"cron": "30 2 * * *"}
    after = datetime(2026, 11, 11, 12, 0, tzinfo=BERLIN)
    assert compute_next_run(s, after) == "2026-11-12T02:30:00+01:00"


def cron_schedule(client, expr, action="start", next_run=None):
    r = client.post("/api/recipes", json=recipe_body()).json()
    s = client.post("/api/schedules", json={"name": "dst", "action": action,
                                            "recipe_id": r["id"], "cron": expr}).json()
    if next_run:
        client.app.state.db.set_schedule_next_run(s["id"], next_run, True)
    return r, s


def test_tick_fall_back_daily_fires_exactly_once(client, monkeypatch):
    r, s = cron_schedule(client, "30 2 * * *", action="restart", next_run="2026-10-25T02:30:00+02:00")
    scheduler = client.app.state.scheduler
    monkeypatch.setattr(sched, "local_now", lambda: datetime(2026, 10, 25, 2, 30, 5, tzinfo=BERLIN))
    client.portal.call(scheduler.tick)
    assert scheduler.runs["ok"] == 1
    assert client.app.state.db.get_schedule(s["id"])["next_run"] == "2026-10-26T02:30:00+01:00"
    # Second pass through the repeated hour: no second run.
    monkeypatch.setattr(sched, "local_now",
                        lambda: datetime(2026, 10, 25, 2, 30, 5, tzinfo=BERLIN, fold=1))
    client.portal.call(scheduler.tick)
    assert scheduler.runs["ok"] == 1
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"


def test_tick_fall_back_hourly_fires_in_both_hours(client, monkeypatch):
    _, s = cron_schedule(client, "0 * * * *", next_run="2026-10-25T02:00:00+02:00")
    scheduler = client.app.state.scheduler
    monkeypatch.setattr(sched, "local_now", lambda: datetime(2026, 10, 25, 2, 0, 5, tzinfo=BERLIN))
    client.portal.call(scheduler.tick)
    assert scheduler.runs["ok"] == 1
    assert client.app.state.db.get_schedule(s["id"])["next_run"] == "2026-10-25T02:00:00+01:00"
    monkeypatch.setattr(sched, "local_now",
                        lambda: datetime(2026, 10, 25, 2, 0, 5, tzinfo=BERLIN, fold=1))
    client.portal.call(scheduler.tick)
    assert scheduler.runs["ok"] == 2
    assert client.app.state.db.get_schedule(s["id"])["next_run"] == "2026-10-25T03:00:00+01:00"


def test_tick_spring_fires_once_at_the_transition(client, monkeypatch):
    _, s = cron_schedule(client, "30 2 * * *", next_run="2026-03-29T03:00:00+02:00")
    scheduler = client.app.state.scheduler
    monkeypatch.setattr(sched, "local_now", lambda: datetime(2026, 3, 29, 3, 0, 5, tzinfo=BERLIN))
    client.portal.call(scheduler.tick)
    assert scheduler.runs["ok"] == 1
    assert client.app.state.db.get_schedule(s["id"])["next_run"] == "2026-03-30T02:30:00+02:00"


# ---- DOC-21: resync after downtime, and pause / resume ------------------------

NOON = datetime(2026, 11, 11, 12, 0, tzinfo=BERLIN)  # an ordinary day, no DST in play


def test_resync_skips_missed_cron_run(client, monkeypatch):
    monkeypatch.setattr(sched, "local_now", lambda: NOON)
    r, s = cron_schedule(client, "0 8 * * *")
    # A run that fell due while the manager was down.
    client.app.state.db.set_schedule_next_run(s["id"], (NOON - timedelta(hours=4)).isoformat(), True)
    client.app.state.scheduler.resync()
    after = client.app.state.db.get_schedule(s["id"])
    assert datetime.fromisoformat(after["next_run"]) > NOON   # recomputed to the future, not fired
    assert client.app.state.scheduler.runs["ok"] == 0
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "stopped"


def test_resync_keeps_one_shot_within_grace(client, monkeypatch):
    monkeypatch.setattr(sched, "local_now", lambda: NOON)
    run_at = (NOON - timedelta(seconds=config.ONCE_GRACE_SECONDS - 60)).isoformat()
    r, s = create_once(client, run_at)
    client.app.state.scheduler.resync()
    after = client.app.state.db.get_schedule(s["id"])
    assert after["enabled"] == 1 and after["next_run"] is not None   # not expired
    client.portal.call(client.app.state.scheduler.tick)              # still fires, < grace late
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"


def test_resync_expires_one_shot_beyond_grace(client, monkeypatch):
    monkeypatch.setattr(sched, "local_now", lambda: NOON)
    run_at = (NOON - timedelta(seconds=config.ONCE_GRACE_SECONDS + 60)).isoformat()
    _, s = create_once(client, run_at)
    client.app.state.scheduler.resync()
    after = client.app.state.db.get_schedule(s["id"])
    assert after["enabled"] == 0 and after["next_run"] is None
    assert after["last_ok"] == 0 and "Missed" in after["last_message"]


def test_pause_and_resume_via_settings(client, monkeypatch):
    monkeypatch.setattr(sched, "local_now", lambda: NOON)
    r, _ = create_once(client, NOON.isoformat())          # a one-shot due right now
    assert client.get("/api/settings").json() == {"schedules_paused": False}
    assert client.put("/api/settings", json={"schedules_paused": True}).json() == {"schedules_paused": True}
    assert client.get("/api/settings").json() == {"schedules_paused": True}
    client.portal.call(client.app.state.scheduler.tick)
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "stopped"   # paused: no fire
    assert client.put("/api/settings", json={"schedules_paused": False}).json() == {"schedules_paused": False}
    client.portal.call(client.app.state.scheduler.tick)
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"     # resumed: fires


def test_resume_realigns_cron_without_burst(client, monkeypatch):
    monkeypatch.setattr(sched, "local_now", lambda: NOON)
    _, s = cron_schedule(client, "0 * * * *")             # hourly
    client.put("/api/settings", json={"schedules_paused": True})
    later = NOON + timedelta(hours=2)                     # two hours pass while paused
    monkeypatch.setattr(sched, "local_now", lambda: later)
    client.put("/api/settings", json={"schedules_paused": False})   # resync realigns
    after = client.app.state.db.get_schedule(s["id"])
    assert datetime.fromisoformat(after["next_run"]) > later        # next future slot, not past ones
    client.portal.call(client.app.state.scheduler.tick)
    assert client.app.state.scheduler.runs["ok"] == 0               # missed runs are not burst-fired
