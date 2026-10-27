"""Database housekeeping: retention of activity log / start times, recorded schema version."""

from datetime import datetime, timedelta

from local_llm_manager.db import Database, SCHEMA_VERSION


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
