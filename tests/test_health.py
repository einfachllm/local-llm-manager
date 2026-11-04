import asyncio
from datetime import datetime, timedelta

from local_llm_manager import health
from local_llm_manager.health import CRITICAL, WARNING, Condition, Tracker

from conftest import recipe_body


def host(hid=1, online=True, avail=60.0, total=121.0, temp=60, is_local=True, error=None):
    return {"id": hid, "name": f"spark-{hid:02d}", "online": online, "is_local": is_local, "error": error,
            "system": {"memory": {"total_gb": total, "available_gb": avail},
                       "gpus": [{"index": "0", "temperature": temp}]} if online else None}


def rules(conds):
    return {(c.rule, c.severity) for c in conds}


# ---- rules ------------------------------------------------------------------


def test_healthy_host_has_no_conditions():
    assert health.host_conditions([host()]) == []


def test_memory_thresholds_are_relative_to_the_total():
    assert rules(health.host_conditions([host(avail=9)])) == {("mem_low", WARNING)}
    assert rules(health.host_conditions([host(avail=3)])) == {("mem_low", CRITICAL)}
    # 9 GB free on a 16 GB laptop is plenty.
    assert health.host_conditions([host(avail=9, total=16)]) == []


def test_gpu_temperature():
    assert rules(health.host_conditions([host(temp=86)])) == {("gpu_hot", WARNING)}
    assert rules(health.host_conditions([host(temp=91)])) == {("gpu_hot", CRITICAL)}
    no_sensor = host()
    no_sensor["system"]["gpus"] = [{"index": "0", "temperature": None}]
    assert health.host_conditions([no_sensor]) == []


def test_offline_agent_but_never_the_hub_itself():
    assert rules(health.host_conditions([host(2, online=False, is_local=False, error="timeout")])) == {
        ("host_offline", CRITICAL)}
    assert health.host_conditions([host(1, online=False, is_local=True)]) == []


def test_kv_cache_needs_waiting_requests():
    r = {"id": 5, "name": "nemotron", "host_id": 1, "status": "ready", "metrics": {"kv_cache": 90, "waiting": 3}}
    assert rules(health.recipe_conditions([r], {}, {})) == {("kv_cache_high", WARNING)}
    r["metrics"]["waiting"] = 0  # a full cache that still keeps up is fine
    assert health.recipe_conditions([r], {}, {}) == []


def test_failed_start_names_the_cause():
    r = {"id": 7, "name": "gemma", "host_id": 1, "status": "failed",
         "container": {"id": "abc", "status": "Exited (1) 2 minutes ago"}}
    [c] = health.recipe_conditions([r], {7: ("CUDA out of memory", "torch.OutOfMemoryError: ...")}, {})
    assert (c.rule, c.severity) == ("start_failed", CRITICAL)
    assert c.message == "CUDA out of memory (exit code 1)" and c.detail.startswith("torch")
    assert c.label == "CUDA out of memory"
    [c] = health.recipe_conditions([r], {}, {})
    assert c.message == "gemma exit code 1" and c.label == ""
    r["container"] = {"id": "1", "exit_code": 137, "status": "Exited (137) 1 minute ago"}
    assert health.recipe_conditions([r], {}, {})[0].message == "gemma exit code 137"


def test_fail_reason_patterns():
    assert health.fail_reason("loading...\ntorch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB\n")[0] \
        == "CUDA out of memory"
    assert health.fail_reason("ValueError: No available memory for the cache blocks.")[0] \
        == "Not enough memory for the KV cache"
    assert health.fail_reason("OSError: [Errno 98] Address already in use")[0] == "Port already in use"
    assert health.fail_reason("llama_model_load: error loading model: failed to load model")[0] == "Model not found"
    assert health.fail_reason("vllm: error: unrecognized arguments: --foo")[0] == "Unknown argument"
    assert health.fail_reason("all good\n") == ("", "")


def test_slow_start_against_its_own_history():
    now = datetime(2026, 11, 4, 12, 0).astimezone()
    history = [{"recipe_id": 1, "seconds": s} for s in (100, 120, 110)]
    started = (now - timedelta(seconds=400)).isoformat()
    assert health.slow_starts({1: (started, "user")}, history, now) == {1: (400.0, 110)}
    recent = (now - timedelta(seconds=200)).isoformat()
    assert health.slow_starts({1: (recent, "user")}, history, now) == {}
    # Too little history: no guess.
    assert health.slow_starts({1: (started, "user")}, history[:2], now) == {}


# ---- hysteresis and activity log ----------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def tracker():
    events, clock = [], Clock()
    t = Tracker(lambda *a: events.append(a), clock)
    return t, events, clock


def cond(rule="gpu_hot", severity=WARNING, msg="GPU at 86 °C"):
    return Condition(rule, f"{rule}:1", severity, "spark-01", msg, host_id=1)


def test_finding_only_after_the_hold_time():
    t, events, clock = tracker()
    t.update([cond()], {})
    assert t.findings() == [] and events == []
    clock.t += health.RULES["gpu_hot"]["hold"]
    t.update([cond()], {})
    [f] = t.findings()
    assert f.severity == WARNING
    assert events[-1][:3] == ("health", "warning", False)


def test_short_blip_never_counts():
    t, events, clock = tracker()
    t.update([cond()], {})
    clock.t += 100
    t.update([], {})  # gone before the hold time: forgotten
    clock.t += 100
    t.update([cond()], {})
    assert t.findings() == [] and events == []


def test_resolved_only_after_clear_time_and_logged():
    t, events, clock = tracker()
    c = cond("start_failed", CRITICAL, "CUDA out of memory")  # hold 0
    t.update([c], {})
    assert len(t.findings()) == 1
    clock.t += 10
    t.update([], {})
    assert len(t.findings()) == 1  # not flapping off right away
    clock.t += health.CLEAR_SECONDS
    t.update([], {})
    assert t.findings() == []
    assert [e[1] for e in events] == ["critical", "resolved"]
    assert events[-1][2] is True


def test_escalation_is_logged_once():
    t, events, clock = tracker()
    t.update([cond()], {})
    clock.t += 200
    t.update([cond()], {})
    t.update([cond(severity=CRITICAL, msg="GPU at 91 °C")], {})
    t.update([cond(severity=CRITICAL, msg="GPU at 92 °C")], {})
    assert [e[1] for e in events] == ["warning", "critical"]
    assert t.findings()[0].message == "GPU at 92 °C"


def test_muted_findings_stay_visible_but_quiet():
    t, events, clock = tracker()
    c = cond("start_failed", CRITICAL)
    t.update([c], {c.key: "2099-01-01T00:00:00+00:00"})
    [f] = t.findings()
    assert f.public()["muted"] and events == []


# ---- through the app ----------------------------------------------------------


def test_failed_recipe_finding_via_api(client, fake_docker):
    r = client.post("/api/recipes", json=recipe_body()).json()
    client.post(f"/api/recipes/{r['id']}/start")
    fake_docker.containers[r["container_name"]].update(state="exited", status="Exited (1) 1 minute ago")
    fake_docker.log_stats = "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB"
    monitor = client.app.state.health
    asyncio.run(monitor.check())

    [f] = client.get("/api/health").json()["findings"]
    assert (f["rule"], f["severity"], f["recipe_id"]) == ("start_failed", "critical", r["id"])
    assert f["label"] == "CUDA out of memory" and "OutOfMemoryError" in f["detail"]
    listed = next(x for x in client.get("/api/recipes").json() if x["id"] == r["id"])
    assert listed["findings"][0]["key"] == f["key"]
    assert client.get("/api/hosts").json()[0]["findings"] == []  # recipe findings stay with the recipe
    ev = client.get("/api/events").json()[0]
    assert (ev["source"], ev["action"]) == ("health", "critical") and not ev["ok"]

    assert client.post("/api/health/mute", json={"key": f["key"], "hours": 1}).status_code == 200
    assert client.get("/api/health").json()["findings"][0]["muted"] is True
    assert client.post("/api/health/unmute", json={"key": f["key"]}).status_code == 204
    assert client.get("/api/health").json()["findings"][0]["muted"] is False


def test_host_memory_finding_via_api(client, monkeypatch):
    from local_llm_manager import executor as executor_mod
    monkeypatch.setattr(executor_mod.system, "read_meminfo",
                        lambda: {"total_gb": 128, "available_gb": 2, "used_gb": 126})
    monitor = client.app.state.health
    asyncio.run(monitor.check())
    monitor.tracker.clock = lambda: 10 ** 9  # past the hold time
    asyncio.run(monitor.check())
    [f] = client.get("/api/hosts").json()[0]["findings"]
    assert (f["rule"], f["severity"]) == ("mem_low", "critical")
