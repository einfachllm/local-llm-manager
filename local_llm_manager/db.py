"""SQLite storage for recipes, schedules and the activity log."""

import os
import socket
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import config

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
    runtime         TEXT NOT NULL DEFAULT 'docker',
    host_id         INTEGER,
    pinned          INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL DEFAULT 'model',
    weights_id      INTEGER,
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
    host_id       INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hosts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL DEFAULT '',   -- '' = this machine
    token       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Model weights registered once, with where they live on each host (Weights tab). A recipe that selects
-- an entry mounts that host's path wherever it writes {weights}, so moving it to another host needs no edits.
CREATE TABLE IF NOT EXISTS weights (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL DEFAULT '',   -- e.g. the Hugging Face repo id, for reference
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weight_locations (
    weight_id  INTEGER NOT NULL REFERENCES weights(id) ON DELETE CASCADE,
    host_id    INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    PRIMARY KEY (weight_id, host_id)
);

CREATE TABLE IF NOT EXISTS start_times (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id  INTEGER NOT NULL,
    seconds    REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    ts         TEXT NOT NULL
);
"""

# Stored in SQLite's user_version pragma so a future migration pass can tell which databases
# already have the current layout. Bump when the schema (not just a column) changes.
SCHEMA_VERSION = 1

RECIPE_FIELDS = (
    "name", "description", "framework", "image", "model", "container_name", "host_port",
    "container_port", "command", "args", "env", "volumes", "docker_args", "gpus",
    "health_path", "est_memory_gb", "tags", "color", "runtime", "host_id", "pinned", "kind", "weights_id",
)

# Columns added after the first release, created on existing databases at startup.
MIGRATIONS = {
    ("recipes", "tags"): "ALTER TABLE recipes ADD COLUMN tags TEXT NOT NULL DEFAULT ''",
    ("recipes", "color"): "ALTER TABLE recipes ADD COLUMN color TEXT NOT NULL DEFAULT ''",
    ("recipes", "runtime"): "ALTER TABLE recipes ADD COLUMN runtime TEXT NOT NULL DEFAULT 'docker'",
    # NULL = the local host (also for recipes created before hosts existed).
    ("recipes", "host_id"): "ALTER TABLE recipes ADD COLUMN host_id INTEGER",
    # Host for stop_all / free_memory schedules; NULL = all hosts.
    ("schedules", "host_id"): "ALTER TABLE schedules ADD COLUMN host_id INTEGER",
    ("recipes", "pinned"): "ALTER TABLE recipes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
    # "model" (an LLM server) or "agent" (a program using one, e.g. a Hermes gateway).
    ("recipes", "kind"): "ALTER TABLE recipes ADD COLUMN kind TEXT NOT NULL DEFAULT 'model'",
    # Registered weights (Weights tab) whose per-host path fills {weights}; NULL = none.
    ("recipes", "weights_id"): "ALTER TABLE recipes ADD COLUMN weights_id INTEGER",
}

SCHEDULE_FIELDS = ("name", "action", "recipe_id", "cron", "run_at", "enabled", "host_id")


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


# NOT NULL columns with a schema default; a missing or None input value would
# otherwise be inserted as NULL and violate the constraint (create_recipe and
# update_recipe always write the full RECIPE_FIELDS list).
RECIPE_DEFAULTS = {
    "description": "", "framework": "", "image": "", "model": "",
    "command": "", "args": "", "env": "", "volumes": "", "docker_args": "",
    "gpus": "all", "health_path": "/health", "color": "", "runtime": "docker", "pinned": 0, "kind": "model",
}


def _recipe_values(data: dict[str, Any]) -> tuple:
    # Tags are stored as a comma-separated string and exposed as a list.
    def value(field: str) -> Any:
        v = data.get(field)
        if v is None and field in RECIPE_DEFAULTS:
            return RECIPE_DEFAULTS[field]
        return v

    return tuple(
        (1 if value("pinned") else 0) if f == "pinned"
        else ",".join(normalize_tags(value(f))) if f == "tags"
        else value(f)
        for f in RECIPE_FIELDS
    )


def _recipe_out(row: dict, local_host_id: int) -> dict:
    row["tags"] = normalize_tags(row.get("tags"))
    row["host_id"] = row.get("host_id") or local_host_id
    return row


class Database:
    def __init__(self, path: Path | str, retention_days: Optional[int] = None):
        self.path = str(path)
        self.retention_days = config.RETENTION_DAYS if retention_days is None else retention_days
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # The database holds agent tokens: readable by the owner only.
            Path(self.path).touch(mode=0o600, exist_ok=True)
            os.chmod(self.path, 0o600)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            for (table, column), sql in MIGRATIONS.items():
                existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._conn.execute(sql)
            if not self._conn.execute("SELECT 1 FROM hosts WHERE url = ''").fetchone():
                self._conn.execute("INSERT INTO hosts (name, url, token, created_at) VALUES (?, '', '', ?)",
                                   (socket.gethostname().split(".")[0] or "local", now_iso()))
            # Record which layout this database has (no-op when it is already current).
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._prune_old_rows()
            self._conn.commit()

    def _prune_old_rows(self) -> None:
        """Drop activity-log and start-time rows older than the retention window (0 = keep all).

        Runs on every start-up, so the database stays bounded; it is a cheap index-free scan of
        two tables that only the hub writes.
        """
        if self.retention_days <= 0:
            return
        cutoff = (datetime.now().astimezone() - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._conn.execute("DELETE FROM start_times WHERE ts < ?", (cutoff,))

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
        local = self.local_host_id()
        return [_recipe_out(r, local) for r in self._query("SELECT * FROM recipes ORDER BY name COLLATE NOCASE")]

    def get_recipe(self, recipe_id: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
        return _recipe_out(rows[0], self.local_host_id()) if rows else None

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

    # ---- settings ------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def clear_setting(self, key: str) -> None:
        self._execute("DELETE FROM settings WHERE key = ?", (key,))

    # ---- start-time tracking -------------------------------------------

    def set_pending_start(self, recipe_id: int, ts: str, source: str) -> None:
        self.set_setting(f"pending_start:{recipe_id}", f"{ts}|{source}")

    def clear_pending_start(self, recipe_id: int) -> None:
        self.clear_setting(f"pending_start:{recipe_id}")

    def pending_starts(self) -> dict[int, tuple[str, str]]:
        """recipe id -> (start timestamp, source) for starts waiting to become ready."""
        out: dict[int, tuple[str, str]] = {}
        for r in self._query("SELECT key, value FROM settings WHERE key LIKE 'pending_start:%'"):
            _, _, rid = r["key"].partition(":")
            ts, _, source = r["value"].partition("|")
            out[int(rid)] = (ts, source)
        return out

    def add_start_time(self, recipe_id: int, seconds: float, source: str) -> None:
        self._execute(
            "INSERT INTO start_times (recipe_id, seconds, source, ts) VALUES (?, ?, ?, ?)",
            (recipe_id, round(seconds, 1), source, now_iso()),
        )

    def list_start_times(self, limit: int = 200) -> list[dict]:
        return self._query("SELECT recipe_id, seconds, source, ts FROM start_times ORDER BY id DESC LIMIT ?", (limit,))

    # ---- hosts ---------------------------------------------------------

    def list_hosts(self) -> list[dict]:
        # Local machine first, then by name.
        return self._query("SELECT * FROM hosts ORDER BY url != '', name COLLATE NOCASE")

    def get_host(self, host_id: int) -> Optional[dict]:
        rows = self._query("SELECT * FROM hosts WHERE id = ?", (host_id,))
        return rows[0] if rows else None

    def local_host_id(self) -> int:
        return self._query("SELECT id FROM hosts WHERE url = '' ORDER BY id LIMIT 1")[0]["id"]

    def create_host(self, name: str, url: str, token: str) -> int:
        return self._execute("INSERT INTO hosts (name, url, token, created_at) VALUES (?, ?, ?, ?)",
                             (name, url, token, now_iso()))

    def update_host(self, host_id: int, name: str, url: str, token: str) -> None:
        self._execute("UPDATE hosts SET name = ?, url = ?, token = ? WHERE id = ?", (name, url, token, host_id))

    def delete_host(self, host_id: int) -> None:
        self._execute("DELETE FROM hosts WHERE id = ?", (host_id,))

    def count_host_recipes(self, host_id: int) -> int:
        local = host_id == self.local_host_id()
        sql = "SELECT COUNT(*) AS n FROM recipes WHERE host_id = ?" + (" OR host_id IS NULL" if local else "")
        return self._query(sql, (host_id,))[0]["n"]

    # ---- weights -------------------------------------------------------

    def list_weights(self) -> list[dict]:
        """Every registered weights entry with `locations` {host_id: path} and the ids of the recipes using it."""
        weights = self._query("SELECT * FROM weights ORDER BY name COLLATE NOCASE")
        locs = self._query("SELECT * FROM weight_locations")
        users = self._query("SELECT id, weights_id FROM recipes WHERE weights_id IS NOT NULL")
        for w in weights:
            w["locations"] = {l["host_id"]: l["path"] for l in locs if l["weight_id"] == w["id"]}
            w["recipe_ids"] = [r["id"] for r in users if r["weights_id"] == w["id"]]
        return weights

    def get_weight(self, weight_id: int) -> Optional[dict]:
        return next((w for w in self.list_weights() if w["id"] == weight_id), None)

    def weight_path(self, weight_id: Optional[int], host_id: int) -> Optional[str]:
        if not weight_id:
            return None
        rows = self._query("SELECT path FROM weight_locations WHERE weight_id = ? AND host_id = ?", (weight_id, host_id))
        return rows[0]["path"] if rows else None

    def _set_locations(self, weight_id: int, locations: dict[int, str]) -> None:
        # Caller holds the lock. An empty path removes the host's entry.
        self._conn.execute("DELETE FROM weight_locations WHERE weight_id = ?", (weight_id,))
        for host_id, path in locations.items():
            if path.strip():
                self._conn.execute("INSERT INTO weight_locations (weight_id, host_id, path) VALUES (?, ?, ?)",
                                   (weight_id, host_id, path.strip()))

    def create_weight(self, name: str, source: str, locations: dict[int, str]) -> int:
        with self._lock:
            wid = self._conn.execute("INSERT INTO weights (name, source, created_at) VALUES (?, ?, ?)",
                                     (name, source, now_iso())).lastrowid
            self._set_locations(wid, locations)
            self._conn.commit()
            return wid

    def update_weight(self, weight_id: int, name: str, source: str, locations: dict[int, str]) -> None:
        with self._lock:
            self._conn.execute("UPDATE weights SET name = ?, source = ? WHERE id = ?", (name, source, weight_id))
            self._set_locations(weight_id, locations)
            self._conn.commit()

    def set_weight_location(self, weight_id: int, host_id: int, path: str) -> None:
        if path.strip():
            self._execute("INSERT INTO weight_locations (weight_id, host_id, path) VALUES (?, ?, ?) "
                          "ON CONFLICT(weight_id, host_id) DO UPDATE SET path = excluded.path",
                          (weight_id, host_id, path.strip()))
        else:
            self._execute("DELETE FROM weight_locations WHERE weight_id = ? AND host_id = ?", (weight_id, host_id))

    def delete_weight(self, weight_id: int) -> None:
        self._execute("DELETE FROM weights WHERE id = ?", (weight_id,))

    # ---- events --------------------------------------------------------

    def add_event(self, source: str, action: str, ok: bool, message: str = "",
                  recipe_id: Optional[int] = None, target: Optional[str] = None) -> None:
        self._execute(
            "INSERT INTO events (ts, source, action, recipe_id, target, ok, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), source, action, recipe_id, target, int(ok), message),
        )

    def list_events(self, limit: int = 200) -> list[dict]:
        return self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---- self-monitoring ------------------------------------------------

    def ping(self) -> bool:
        """True if the database answers a trivial query (for the /healthz endpoint)."""
        try:
            self._query("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def counts(self) -> dict[str, int]:
        """How many recipes / hosts / schedules are stored (for the /metrics endpoint)."""
        out = {table: self._query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
               for table in ("recipes", "hosts", "schedules")}
        out["schedules_enabled"] = self._query("SELECT COUNT(*) AS n FROM schedules WHERE enabled = 1")[0]["n"]
        return out
