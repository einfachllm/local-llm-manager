"""SQLite storage for recipes, schedules and the activity log."""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    framework       TEXT NOT NULL,
    image           TEXT NOT NULL,
    model           TEXT NOT NULL DEFAULT '',
    container_name  TEXT NOT NULL UNIQUE,
    host_port       INTEGER NOT NULL,
    container_port  INTEGER NOT NULL,
    command         TEXT NOT NULL DEFAULT '',
    args            TEXT NOT NULL DEFAULT '',
    env             TEXT NOT NULL DEFAULT '',
    volumes         TEXT NOT NULL DEFAULT '',
    docker_args     TEXT NOT NULL DEFAULT '',
    gpus            TEXT NOT NULL DEFAULT 'all',
    health_path     TEXT NOT NULL DEFAULT '/health',
    est_memory_gb   REAL,
    tags            TEXT NOT NULL DEFAULT '',
    color           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    action        TEXT NOT NULL,
    recipe_id     INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
    cron          TEXT,
    run_at        TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_run      TEXT,
    last_run      TEXT,
    last_ok       INTEGER,
    last_message  TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    source     TEXT NOT NULL,
    action     TEXT NOT NULL,
    recipe_id  INTEGER,
    target     TEXT,
    ok         INTEGER NOT NULL,
    message    TEXT NOT NULL DEFAULT ''
);
"""

RECIPE_FIELDS = (
    "name", "description", "framework", "image", "model", "container_name", "host_port",
    "container_port", "command", "args", "env", "volumes", "docker_args", "gpus",
    "health_path", "est_memory_gb", "tags", "color",
)

# Columns added after the first release, created on existing databases at startup.
RECIPE_MIGRATIONS = {
    "tags": "ALTER TABLE recipes ADD COLUMN tags TEXT NOT NULL DEFAULT ''",
    "color": "ALTER TABLE recipes ADD COLUMN color TEXT NOT NULL DEFAULT ''",
}

SCHEDULE_FIELDS = ("name", "action", "recipe_id", "cron", "run_at", "enabled")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_tags(tags: Any) -> list[str]:
    """Trimmed, whitespace-collapsed, de-duplicated (case-insensitively) tags in input order."""
    if isinstance(tags, str):
        tags = tags.split(",")
    out, seen = [], set()
    for t in tags or []:
        t = " ".join(str(t).replace(",", " ").split())[:40]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _recipe_values(data: dict[str, Any]) -> tuple:
    # Tags are stored as a comma-separated string and exposed as a list.
    return tuple(",".join(normalize_tags(data.get(f))) if f == "tags" else data.get(f) for f in RECIPE_FIELDS)


def _recipe_out(row: dict) -> dict:
    row["tags"] = normalize_tags(row.get("tags"))
    return row


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(recipes)")}
            for column, sql in RECIPE_MIGRATIONS.items():
                if column not in existing:
                    self._conn.execute(sql)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    # ---- recipes -------------------------------------------------------

    def list_recipes(self) -> list[dict]:
        return [_recipe_out(r) for r in self._query("SELECT * FROM recipes ORDER BY name COLLATE NOCASE")]

    def get_recipe(self, recipe_id: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
        return _recipe_out(rows[0]) if rows else None

    def create_recipe(self, data: dict[str, Any]) -> int:
        ts = now_iso()
        cols = ", ".join(RECIPE_FIELDS)
        marks = ", ".join("?" for _ in RECIPE_FIELDS)
        return self._execute(
            f"INSERT INTO recipes ({cols}, created_at, updated_at) VALUES ({marks}, ?, ?)",
            _recipe_values(data) + (ts, ts),
        )

    def update_recipe(self, recipe_id: int, data: dict[str, Any]) -> None:
        sets = ", ".join(f"{f} = ?" for f in RECIPE_FIELDS)
        self._execute(
            f"UPDATE recipes SET {sets}, updated_at = ? WHERE id = ?",
            _recipe_values(data) + (now_iso(), recipe_id),
        )

    def delete_recipe(self, recipe_id: int) -> None:
        self._execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))

    # ---- schedules -----------------------------------------------------

    def list_schedules(self) -> list[dict]:
        return self._query("SELECT * FROM schedules ORDER BY id")

    def get_schedule(self, schedule_id: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return rows[0] if rows else None

    def create_schedule(self, data: dict[str, Any], next_run: Optional[str]) -> int:
        cols = ", ".join(SCHEDULE_FIELDS)
        marks = ", ".join("?" for _ in SCHEDULE_FIELDS)
        return self._execute(
            f"INSERT INTO schedules ({cols}, next_run, created_at) VALUES ({marks}, ?, ?)",
            tuple(data.get(f) for f in SCHEDULE_FIELDS) + (next_run, now_iso()),
        )

    def update_schedule(self, schedule_id: int, data: dict[str, Any], next_run: Optional[str]) -> None:
        sets = ", ".join(f"{f} = ?" for f in SCHEDULE_FIELDS)
        self._execute(
            f"UPDATE schedules SET {sets}, next_run = ? WHERE id = ?",
            tuple(data.get(f) for f in SCHEDULE_FIELDS) + (next_run, schedule_id),
        )

    def set_schedule_next_run(self, schedule_id: int, next_run: Optional[str], enabled: bool) -> None:
        self._execute(
            "UPDATE schedules SET next_run = ?, enabled = ? WHERE id = ?",
            (next_run, int(enabled), schedule_id),
        )

    def record_schedule_run(self, schedule_id: int, ok: bool, message: str) -> None:
        self._execute(
            "UPDATE schedules SET last_run = ?, last_ok = ?, last_message = ? WHERE id = ?",
            (now_iso(), int(ok), message, schedule_id),
        )

    def delete_schedule(self, schedule_id: int) -> None:
        self._execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

    # ---- events --------------------------------------------------------

    def add_event(self, source: str, action: str, ok: bool, message: str = "",
                  recipe_id: Optional[int] = None, target: Optional[str] = None) -> None:
        self._execute(
            "INSERT INTO events (ts, source, action, recipe_id, target, ok, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), source, action, recipe_id, target, int(ok), message),
        )

    def list_events(self, limit: int = 200) -> list[dict]:
        return self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
