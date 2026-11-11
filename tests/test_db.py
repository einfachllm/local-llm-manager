"""Database housekeeping: retention of activity log / start times, recorded schema version."""

from datetime import datetime, timedelta

import pytest

from local_llm_manager.db import Database, SCHEMA_VERSION, WeightExists


def _old_iso(days: int) -> str:
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def test_retention_prunes_old_events_and_start_times(tmp_path):
    path = tmp_path / "m.db"
    db = Database(path)
    db.add_event("user", "start", True, "recent event")
    db.add_start_time(1, 30.0, "user")
    db._execute("INSERT INTO events (ts, source, action, ok, message) VALUES (?, 'user', 'stop', 1, 'old event')",
                (_old_iso(120),))
    db._execute("INSERT INTO start_times (recipe_id, seconds, source, ts) VALUES (?, ?, ?, ?)",
                (1, 30.0, "user", _old_iso(120)))

    # A fresh start (the normal path: config default of 90 days) keeps the recent rows only.
    db2 = Database(path)
    assert [e["message"] for e in db2.list_events()] == ["recent event"]
    assert len(db2.list_start_times()) == 1


def test_retention_days_can_be_set_or_disabled(tmp_path):
    path = tmp_path / "m.db"
    db = Database(path)
    db.add_event("user", "start", True, "keep me")
    db._execute("INSERT INTO events (ts, source, action, ok, message) VALUES (?, 'user', 'stop', 1, 'old event')",
                (_old_iso(120),))

    assert [e["message"] for e in Database(path, retention_days=365).list_events()] == ["old event", "keep me"]
    assert len(Database(path, retention_days=0).list_events()) == 2  # 0 = keep everything


def test_clear_events(tmp_path):
    db = Database(tmp_path / "m.db")
    db.add_event("user", "start", True, "one")
    db.add_event("user", "stop", True, "two")
    assert len(db.list_events()) == 2
    db.clear_events()
    assert db.list_events() == []


def test_seed_examples_adds_the_qwen38_example(tmp_path):
    from local_llm_manager.main import seed_examples

    db = Database(tmp_path / "m.db")
    seed_examples(db)
    recipes = db.list_recipes()
    assert [r["name"] for r in recipes] == ["qwen3.8-mtp"]
    assert recipes[0]["framework"] == "vllm"
    assert recipes[0]["host_id"] == db.local_host_id()
    # The example points at a registered weights entry, created without a path on a fresh install.
    weights = db.list_weights()
    assert [w["name"] for w in weights] == ["Qwen3.8-27B-NVFP4"]
    assert recipes[0]["weights_id"] == weights[0]["id"]
    # Idempotent: a second call on a non-empty database adds nothing.
    seed_examples(db)
    assert len(db.list_recipes()) == 1


def test_list_events_filters_by_source(tmp_path):
    """The activity log can be filtered by source - the UI's Health filter uses ?source=health (UI-17)."""
    db = Database(tmp_path / "m.db")
    db.add_event("user", "start", True, "u")
    db.add_event("health", "mem_low", False, "h")
    db.add_event("schedule", "stop", True, "s")
    assert [e["message"] for e in db.list_events(source="health")] == ["h"]
    assert len(db.list_events()) == 3


def test_events_endpoint_source_filter(client):
    client.app.state.db.add_event("health", "mem_low", False, "h")
    client.app.state.db.add_event("user", "start", True, "u")
    r = client.get("/api/events?source=health")
    assert r.status_code == 200
    assert [e["message"] for e in r.json()] == ["h"]
    assert len(client.get("/api/events").json()) >= 2


def test_old_db_with_case_duplicates_starts_and_refuses_new(tmp_path):
    """A 0.2.0 database holding qwen3.8/Qwen3.8 and slash-spelled paths: the new code starts cleanly,
    the case-insensitive index falls back (old rows stay), paths are normalized, and the API refuses
    new duplicates by name and by folder (DOC-24)."""
    path = tmp_path / "old.db"
    db = Database(path)
    # Simulate a 0.2.0 database: no CI index yet, and rows bypassing today's API checks.
    db._execute("DROP INDEX weights_name_ci")
    db._execute("INSERT INTO weights (name, source, created_at) VALUES ('qwen3.8', '', '2026-01-01T00:00:00+00:00')")
    db._execute("INSERT INTO weights (name, source, created_at) VALUES ('Qwen3.8', '', '2026-01-01T00:00:00+00:00')")
    hid = db.local_host_id()
    db._execute("INSERT INTO weight_locations (weight_id, host_id, path) VALUES (1, ?, '~/models/x/')", (hid,))
    db._execute("INSERT INTO weight_locations (weight_id, host_id, path) VALUES (2, ?, '~/models//y')", (hid,))
    del db

    db2 = Database(path)  # must start cleanly despite the duplicates
    assert len(db2.list_weights()) == 2
    locs = {w["name"]: w["locations"][hid] for w in db2.list_weights()}
    assert locs["qwen3.8"] == "~/models/x" and locs["Qwen3.8"] == "~/models/y"
    # The CI index could not be created over the old duplicates - the API check still refuses new ones.
    assert db2._query("SELECT name FROM sqlite_master WHERE type='index' AND name='weights_name_ci'") == []
    with pytest.raises(WeightExists):
        db2.create_weight("QWEN3.8", "", {})
    with pytest.raises(WeightExists):
        db2.create_weight("other", "", {hid: "~/models/x"})


def test_delete_recipe_clears_pending_start(tmp_path):
    """A deleted recipe leaves no pending start measurement behind (CORE-09)."""
    db = Database(tmp_path / "m.db")
    rid = db.create_recipe({"name": "x", "container_name": "x", "framework": "vllm", "image": "i",
                            "host_port": 8000, "container_port": 8000})
    db.set_pending_start(rid, "2026-01-01T00:00:00+00:00", "user")
    assert rid in db.pending_starts()
    db.delete_recipe(rid)
    assert rid not in db.pending_starts()


def test_db_path_default_is_cwd_relative(monkeypatch, tmp_path):
    """Without DGXM_DB the database lives in data/ below the working directory; DGXM_DB overrides."""
    import importlib
    import os
    from pathlib import Path

    from local_llm_manager import config

    original = os.environ.get("DGXM_DB")
    monkeypatch.delenv("DGXM_DB", raising=False)
    try:
        importlib.reload(config)
        assert config.DB_PATH == Path("data/manager.db")
        assert not config.DB_PATH.is_absolute()
        assert config.PROC_DIR == Path("data/processes")
        monkeypatch.setenv("DGXM_DB", str(tmp_path / "custom" / "x.db"))
        importlib.reload(config)
        assert config.DB_PATH == tmp_path / "custom" / "x.db"
        assert config.PROC_DIR == tmp_path / "custom" / "processes"
    finally:
        if original is None:
            os.environ.pop("DGXM_DB", None)
        else:
            os.environ["DGXM_DB"] = original
        importlib.reload(config)


def test_schema_version_is_recorded(tmp_path):
    db = Database(tmp_path / "m.db")
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1


def test_ping_and_counts(tmp_path):
    db = Database(tmp_path / "m.db")
    assert db.ping()
    assert db.counts() == {"recipes": 0, "hosts": 1, "schedules": 0, "schedules_enabled": 0}
    db.create_recipe({"name": "x", "container_name": "x", "framework": "vllm", "image": "i",
                      "host_port": 8000, "container_port": 8000})
    db.create_schedule({"name": "s", "action": "stop_all", "cron": "0 3 * * *", "enabled": 1},
                       "2030-01-01T00:00:00+00:00")
    counts = db.counts()
    assert counts["recipes"] == 1 and counts["schedules"] == 1 and counts["schedules_enabled"] == 1
