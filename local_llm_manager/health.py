"""Health findings: small, explainable rules over readings the hub already collects.

Every rule looks at one snapshot - the hosts (memory, GPUs, online state) and the recipes with their
status and engine metrics - and names the problems it sees as *conditions*. The monitor turns a condition
into a *finding* only once it has held for the rule's `hold` time, and drops the finding once the condition
has been gone for `CLEAR_SECONDS`, so a reading sitting on a threshold neither flaps nor floods the
activity log. New and resolved findings are written to the activity log (source "health").

Rules only read: nothing here starts, stops or changes anything.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from . import config

log = logging.getLogger("local_llm_manager.health")

WARNING, CRITICAL = "warning", "critical"
_RANK = {WARNING: 1, CRITICAL: 2}

# A finding stays until its condition has been absent this long.
CLEAR_SECONDS = 60
# Log lines read from a failed recipe to name the cause.
FAIL_LOG_TAIL = 300

RULES: dict[str, dict[str, Any]] = {
    "host_offline": {"title": "Host unreachable", "hold": 120,
                     "hint": "Check that the manager runs on that machine and the network between hub and agent."},
    "mem_low": {"title": "Memory almost full", "hold": 60,
                "hint": "Stop a model you don't need, or use Switch to: it stops the others and frees memory first."},
    "gpu_hot": {"title": "GPU running hot", "hold": 180,
                "hint": "Check airflow and room temperature; a lighter model or fewer parallel requests also helps."},
    "kv_cache_high": {"title": "KV cache almost full", "hold": 300,
                      "hint": "Requests are queueing. Give the engine more memory (e.g. --gpu-memory-utilization) "
                              "or allow fewer / shorter requests (e.g. --max-num-seqs, --max-model-len)."},
    "start_failed": {"title": "Start failed", "hold": 0,
                     "hint": "Open Logs for the full output."},
    "start_slow": {"title": "Start takes unusually long", "hold": 0,
                   "hint": "Check Logs - it may be downloading, compiling, or stuck."},
}

# Memory: share of the total that is still available.
MEM_WARN_FREE, MEM_CRIT_FREE = 0.08, 0.03
GPU_WARN_C, GPU_CRIT_C = 85, 90
KV_WARN_PCT = 85
# A start is slow once it took this many times the median of earlier starts (and at least SLOW_MIN_EXTRA s more).
SLOW_FACTOR, SLOW_MIN_EXTRA, SLOW_MIN_SAMPLES = 2.0, 120, 3

# Known causes of a failed start, in the log of the engine. First match wins.
FAIL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("CUDA out of memory", re.compile(r"CUDA out of memory|OutOfMemoryError|cudaErrorMemoryAllocation|"
                                      r"CUBLAS_STATUS_ALLOC_FAILED|failed to allocate .* (?:buffer|memory)", re.I)),
    ("Not enough memory for the KV cache", re.compile(r"No available memory for the cache blocks|"
                                                      r"larger than the (?:maximum number of tokens|available KV)|"
                                                      r"KV cache is needed, which is larger", re.I)),
    ("Killed (out of memory)", re.compile(r"\bKilled\b|Out of memory: Killed process|exit code 137|oom-kill", re.I)),
    ("Port already in use", re.compile(r"address already in use|bind\(\) failed|EADDRINUSE", re.I)),
    ("Model not found", re.compile(r"No such file or directory.*(?:model|gguf|config\.json)|"
                                   r"is not a local folder and is not a valid model identifier|"
                                   r"Repository Not Found|failed to load model|"
                                   r"does not appear to have a file named", re.I)),
    ("Hugging Face login needed", re.compile(r"gated repo|401 Client Error|Access to model .* is restricted", re.I)),
    ("CUDA / driver error", re.compile(r"CUDA error|no CUDA-capable device|NVIDIA driver|"
                                       r"could not select device driver", re.I)),
    ("Unknown argument", re.compile(r"unrecognized arguments?|error: argument|invalid choice", re.I)),
]


def fail_reason(log_text: str) -> tuple[str, str]:
    """(cause, matching log line) for a failed engine's log; ("", "") when nothing known matches."""
    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
    for cause, pattern in FAIL_PATTERNS:
        # The last match is usually the one that ended the process.
        for line in reversed(lines):
            if pattern.search(line):
                return cause, line[-300:]
    return "", ""


@dataclass
class Condition:
    """One problem seen in one snapshot."""
    rule: str
    key: str                     # stable identity, e.g. "gpu_hot:host:1"
    severity: str
    target: str                  # host or recipe name, for the activity log
    message: str
    host_id: Optional[int] = None
    recipe_id: Optional[int] = None
    detail: str = ""             # e.g. the log line that names the cause
    label: str = ""              # short chip text when it says more than the rule's title


@dataclass
class Finding:
    rule: str
    key: str
    severity: str
    target: str
    message: str
    host_id: Optional[int]
    recipe_id: Optional[int]
    detail: str
    since: str                   # when it became a finding (ISO, local time)
    muted_until: str = ""
    label: str = ""

    def public(self) -> dict[str, Any]:
        r = RULES[self.rule]
        return {"rule": self.rule, "key": self.key, "severity": self.severity, "title": r["title"],
                "label": self.label or r["title"],
                "target": self.target, "message": self.message, "detail": self.detail, "hint": r["hint"],
                "host_id": self.host_id, "recipe_id": self.recipe_id, "since": self.since,
                "muted": bool(self.muted_until), "muted_until": self.muted_until}


# ---- rules ------------------------------------------------------------------


def host_conditions(hosts: list[dict]) -> list[Condition]:
    out: list[Condition] = []
    for h in hosts:
        hid, name = h["id"], h["name"]
        if not h.get("online"):
            if not h.get("is_local"):
                out.append(Condition("host_offline", f"host_offline:{hid}", CRITICAL, name,
                                     f"{name} does not answer" + (f": {h['error']}" if h.get("error") else ""),
                                     host_id=hid))
            continue
        system = h.get("system") or {}
        mem = system.get("memory") or {}
        total, avail = mem.get("total_gb") or 0, mem.get("available_gb")
        if total and avail is not None:
            free = avail / total
            if free < MEM_WARN_FREE:
                sev = CRITICAL if free < MEM_CRIT_FREE else WARNING
                out.append(Condition("mem_low", f"mem_low:{hid}", sev, name,
                                     f"Only {avail:g} of {total:g} GB free on {name}", host_id=hid))
        temps = [(g.get("index", ""), g["temperature"]) for g in system.get("gpus") or []
                 if isinstance(g.get("temperature"), (int, float))]
        if temps:
            idx, hottest = max(temps, key=lambda t: t[1])
            if hottest >= GPU_WARN_C:
                sev = CRITICAL if hottest >= GPU_CRIT_C else WARNING
                gpu = f"GPU {idx}" if len(temps) > 1 else "GPU"
                out.append(Condition("gpu_hot", f"gpu_hot:{hid}", sev, name,
                                     f"{gpu} at {hottest:g} °C on {name} (warning from {GPU_WARN_C} °C, "
                                     f"critical from {GPU_CRIT_C} °C)", host_id=hid))
    return out


def recipe_conditions(recipes: list[dict], fail_causes: dict[int, tuple[str, str]],
                      slow: dict[int, tuple[float, float]]) -> list[Condition]:
    """`fail_causes`: {recipe id: (cause, log line)} of failed recipes; `slow`: {recipe id: (elapsed s, median s)}."""
    out: list[Condition] = []
    for r in recipes:
        rid, name, status = r["id"], r["name"], r.get("status")
        if status == "failed":
            cause, line = fail_causes.get(rid, ("", ""))
            code = _exit_code(r.get("container") or {})
            exited = f"exit code {code}" if code is not None else "exited with an error"
            msg = f"{cause} ({exited})" if cause else f"{name} {exited}"
            out.append(Condition("start_failed", f"start_failed:{rid}", CRITICAL, name, msg,
                                 host_id=r.get("host_id"), recipe_id=rid, detail=line, label=cause))
        m = r.get("metrics") or {}
        kv, waiting = m.get("kv_cache"), m.get("waiting")
        if status == "ready" and isinstance(kv, (int, float)) and kv >= KV_WARN_PCT and (waiting or 0) > 0:
            out.append(Condition("kv_cache_high", f"kv_cache_high:{rid}", WARNING, name,
                                 f"KV cache at {kv:g} % with {waiting} request{'s' if waiting != 1 else ''} waiting",
                                 host_id=r.get("host_id"), recipe_id=rid))
        if rid in slow and status in ("loading", "starting"):
            elapsed, med = slow[rid]
            out.append(Condition("start_slow", f"start_slow:{rid}", WARNING, name,
                                 f"Loading for {round(elapsed / 60)} min, usually ready after about "
                                 f"{max(1, round(med / 60))} min", host_id=r.get("host_id"), recipe_id=rid))
    return out


def _exit_code(container: dict) -> Optional[int]:
    """Exit code of a process (its own field) or a container ("Exited (137) 2 minutes ago")."""
    if isinstance(container.get("exit_code"), int):
        return container["exit_code"]
    m = re.match(r"Exited \((-?\d+)\)", container.get("status") or "")
    return int(m.group(1)) if m else None


def slow_starts(pending: dict[int, tuple[str, str]], history: list[dict],
                now: Optional[datetime] = None) -> dict[int, tuple[float, float]]:
    """Recipes whose current start already took far longer than their usual start."""
    now = now or datetime.now().astimezone()
    per: dict[int, list[float]] = {}
    for row in history:  # newest first
        per.setdefault(row["recipe_id"], []).append(row["seconds"])
    out: dict[int, tuple[float, float]] = {}
    for rid, (ts, _source) in pending.items():
        times = per.get(rid, [])[:8]
        if len(times) < SLOW_MIN_SAMPLES:
            continue
        try:
            elapsed = (now - datetime.fromisoformat(ts)).total_seconds()
        except ValueError:
            continue
        med = sorted(times)[len(times) // 2]
        if elapsed > max(med * SLOW_FACTOR, med + SLOW_MIN_EXTRA):
            out[rid] = (elapsed, med)
    return out


# ---- monitor ----------------------------------------------------------------


@dataclass
class _Pending:
    first_seen: float
    last_seen: float
    cond: Condition


@dataclass
class Tracker:
    """Hysteresis and the activity log: conditions in, findings (and log entries) out."""
    add_event: Callable[..., None]
    clock: Callable[[], float] = time.monotonic
    seen: dict[str, _Pending] = field(default_factory=dict)
    active: dict[str, Finding] = field(default_factory=dict)

    def update(self, conditions: list[Condition], mutes: dict[str, str]) -> None:
        now = self.clock()
        current = {c.key: c for c in conditions}
        for key, c in current.items():
            p = self.seen.get(key)
            if p is None:
                p = self.seen[key] = _Pending(now, now, c)
            p.last_seen, p.cond = now, c
            f = self.active.get(key)
            if f is None:
                if now - p.first_seen >= RULES[c.rule]["hold"]:
                    self.active[key] = f = Finding(c.rule, key, c.severity, c.target, c.message, c.host_id,
                                                   c.recipe_id, c.detail, _now_iso(), mutes.get(key, ""), c.label)
                    self._log(f, f.severity)
            else:
                worse = _RANK[c.severity] > _RANK[f.severity]
                f.severity, f.message, f.target = c.severity, c.message, c.target
                f.detail, f.label = c.detail or f.detail, c.label or f.label
                if worse:
                    self._log(f, f.severity)
        for key in list(self.seen):
            if key in current:
                continue
            p = self.seen[key]
            if now - p.last_seen >= CLEAR_SECONDS or key not in self.active:
                # Not yet a finding: forget it right away, so a blip never counts towards the hold time.
                del self.seen[key]
                f = self.active.pop(key, None)
                if f:
                    self._log(f, "resolved")
        for f in self.active.values():
            f.muted_until = mutes.get(f.key, "")

    def _log(self, f: Finding, action: str) -> None:
        if f.muted_until:
            return
        title = RULES[f.rule]["title"]
        text = f"{title}: {f.message}" if action != "resolved" else f"Resolved - {title.lower()}: {f.message}"
        if f.detail and action != "resolved":
            text += f"\n{f.detail}"
        try:
            self.add_event("health", action, action == "resolved", text, f.recipe_id, f.target)
        except Exception:
            log.exception("could not log health finding")

    def findings(self) -> list[Finding]:
        return sorted(self.active.values(), key=lambda f: (-_RANK[f.severity], f.since))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


MUTES_KEY = "health_mutes"


class HealthMonitor:
    """Runs the rules in the background, also when no browser is open (hold times need that)."""

    def __init__(self, db, manager):
        self.db, self.manager = db, manager
        self.tracker = Tracker(db.add_event)
        self._fail_cache: dict[tuple[int, str], tuple[str, str]] = {}
        self._task: Optional[asyncio.Task] = None
        self.last_run: str = ""

    # mutes: {finding key: until (ISO)}, kept in settings so they survive restarts
    def mutes(self) -> dict[str, str]:
        try:
            data = json.loads(self.db.get_setting(MUTES_KEY) or "{}")
        except ValueError:
            data = {}
        now = datetime.now().astimezone()
        live = {}
        for k, v in data.items():
            try:
                if datetime.fromisoformat(v) > now:
                    live[k] = v
            except (TypeError, ValueError):
                continue
        return live

    def mute(self, key: str, hours: float) -> str:
        mutes = self.mutes()
        until = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
        mutes[key] = until
        self.db.set_setting(MUTES_KEY, json.dumps(mutes))
        if key in self.tracker.active:
            self.tracker.active[key].muted_until = until
        return until

    def unmute(self, key: str) -> None:
        mutes = self.mutes()
        mutes.pop(key, None)
        self.db.set_setting(MUTES_KEY, json.dumps(mutes))
        if key in self.tracker.active:
            self.tracker.active[key].muted_until = ""

    async def _fail_causes(self, recipes: list[dict]) -> dict[int, tuple[str, str]]:
        """Reads the log of each failed recipe once per failed instance."""
        out: dict[int, tuple[str, str]] = {}
        failed = [r for r in recipes if r.get("status") == "failed"]
        live = set()
        for r in failed:
            ck = (r["id"], str((r.get("container") or {}).get("id", "")))
            live.add(ck)
            if ck not in self._fail_cache:
                try:
                    text = await self.manager.logs(r, tail=FAIL_LOG_TAIL)
                except Exception:
                    text = ""
                self._fail_cache[ck] = fail_reason(text)
            out[r["id"]] = self._fail_cache[ck]
        for ck in set(self._fail_cache) - live:
            del self._fail_cache[ck]
        return out

    async def check(self) -> None:
        hosts, recipes = await asyncio.gather(self.manager.host_overview(), self.manager.recipes_with_status())
        offline = {h["id"] for h in hosts if not h.get("online")}
        # A recipe on an unreachable host is covered by host_offline; its status says nothing new.
        recipes = [r for r in recipes if r.get("host_id") not in offline]
        conditions = host_conditions(hosts) + recipe_conditions(
            recipes, await self._fail_causes(recipes),
            slow_starts(self.db.pending_starts(), self.db.list_start_times(500)))
        self.tracker.update(conditions, self.mutes())
        self.last_run = _now_iso()

    def findings(self) -> list[dict[str, Any]]:
        return [f.public() for f in self.tracker.findings()]

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("health check failed")
            await asyncio.sleep(config.HEALTH_TICK)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
