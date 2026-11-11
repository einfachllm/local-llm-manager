import builtins
import json
import os
import socket
import sys
import time

import psutil
from conftest import recipe_body

from local_llm_manager import processes as processes_mod
from local_llm_manager.processes import ProcessRunner


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def native(**kw):
    body = recipe_body(runtime="process", framework="process-custom", image="", model="unused",
                       command=f"{sys.executable} -m http.server {{port}} --bind 127.0.0.1",
                       args="", env="GREETING=hello", volumes="", docker_args="", gpus="", health_path="/",
                       est_memory_gb=None)
    body.update(kw)
    return body


def wait_status(client, rid, want, timeout=10):
    for _ in range(int(timeout / 0.2)):
        st = client.get(f"/api/recipes/{rid}").json()
        if st["status"] in want:
            return st
        time.sleep(0.2)
    return st


def test_validation(client):
    assert client.post("/api/recipes", json=native(framework="vllm")).status_code == 422  # docker framework
    assert client.post("/api/recipes", json=native(command="")).status_code == 422
    assert client.post("/api/recipes", json=recipe_body(image="")).status_code == 422     # docker needs image
    r = client.post("/api/recipes", json=native(host_port=9001, container_port=1234)).json()
    assert r["runtime"] == "process" and r["container_port"] == 9001
    cmd = client.post("/api/recipes/preview", json=native(host_port=9002)).json()["command"]
    assert "GREETING=***" in cmd and "hello" not in cmd and cmd.endswith("-m http.server 9002 --bind 127.0.0.1")


def test_env_values_are_masked_in_preview_and_activity_log(client):
    body = native(host_port=free_port(), env="SECRET=hunter2\nBARE_KEY")
    cmd = client.post("/api/recipes/preview", json=body).json()["command"]
    assert "SECRET=***" in cmd and "hunter2" not in cmd
    r = client.post("/api/recipes", json=body).json()
    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    try:
        events = client.get("/api/events").json()
        assert "SECRET=***" in events[0]["message"] and "hunter2" not in events[0]["message"]
        assert all("hunter2" not in e["message"] for e in events)
    finally:
        client.post(f"/api/recipes/{r['id']}/stop")


def test_start_ready_logs_stop(client):
    port = free_port()
    r = client.post("/api/recipes", json=native(host_port=port)).json()
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 200 and "pid" in res.json()["message"]
    st = wait_status(client, r["id"], {"ready"})
    assert st["status"] == "ready" and "pid" in st["container"]["status"]
    # Already running -> no second process.
    assert "already running" in client.post(f"/api/recipes/{r['id']}/start").json()["message"]
    logs = client.get(f"/api/recipes/{r['id']}/logs").text
    assert "starting:" in logs and "http.server" in logs

    assert client.post(f"/api/recipes/{r['id']}/stop").status_code == 200
    st = client.get(f"/api/recipes/{r['id']}").json()
    assert st["status"] == "stopped" and st["container"]["status"].startswith("Stopped")
    stream = client.get(f"/api/recipes/{r['id']}/logs/stream").text
    assert "starting:" in stream and "event: end" in stream


def test_crash_shows_failed_with_exit_code(client):
    r = client.post("/api/recipes", json=native(
        command=f"{sys.executable} -c \"import sys; print('boom'); sys.exit(3)\"", host_port=free_port())).json()
    client.post(f"/api/recipes/{r['id']}/start")
    st = wait_status(client, r["id"], {"failed"})
    assert st["status"] == "failed" and st["container"]["status"].startswith("Exited (3)")
    assert "boom" in client.get(f"/api/recipes/{r['id']}/logs").text


def test_missing_command_is_reported(client):
    r = client.post("/api/recipes", json=native(command="definitely-not-installed-xyz --port {port}")).json()
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 500 and "Command not found" in res.json()["detail"]


def test_docker_outage_does_not_affect_native_status(client, fake_docker):
    r = client.post("/api/recipes", json=native(host_port=free_port())).json()
    client.post(f"/api/recipes/{r['id']}/start")
    assert wait_status(client, r["id"], {"ready"})["status"] == "ready"
    fake_docker.fail_list_containers = True
    try:
        # Native processes do not ask Docker: their status stays readable during a daemon outage.
        assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"
    finally:
        fake_docker.fail_list_containers = False
        client.post(f"/api/recipes/{r['id']}/stop")


def test_process_survives_new_runner_and_switch_stops_it(client, tmp_path):
    port = free_port()
    a = client.post("/api/recipes", json=native(name="A", host_port=port)).json()
    b = client.post("/api/recipes", json=recipe_body(name="B", host_port=free_port(), health_path="")).json()
    client.post(f"/api/recipes/{a['id']}/start")
    assert wait_status(client, a["id"], {"ready"})["status"] == "ready"

    # A fresh runner (as after a manager restart) still sees the process as ours.
    fresh = ProcessRunner(tmp_path / "processes")
    assert fresh.status(a["container_name"])["state"] == "running"

    # Switching to a docker recipe stops the native one too.
    assert client.post(f"/api/recipes/{b['id']}/switch").status_code == 200
    statuses = {r["name"]: r["status"] for r in client.get("/api/recipes").json()}
    assert statuses == {"A": "stopped", "B": "ready"}


def test_cleanup_state_removes_every_file(tmp_path):
    runner = ProcessRunner(tmp_path / "proc")
    name = "model-x"
    state, log, exit_file = runner._paths(name)
    state.write_text("{}")
    log.write_text("current log")
    log.with_suffix(".log.1").write_text("previous log")
    exit_file.write_text("0")
    (runner.dir / f"{name}.stopped").write_text("abc123")
    runner.cleanup_state(name)
    for p in (state, log, log.with_suffix(".log.1"), exit_file, runner.dir / f"{name}.stopped"):
        assert not p.exists()


def test_cleanup_state_is_safe_when_nothing_exists(tmp_path):
    runner = ProcessRunner(tmp_path / "proc")
    runner.cleanup_state("never-started")  # missing_ok: must not raise


def test_logs_tail_matches_splitlines_semantics(tmp_path):
    runner = ProcessRunner(tmp_path / "proc")
    _, log_file, _ = runner._paths("m")
    log_file.write_text("a\nb\nc\nd\ne")  # no trailing newline
    assert runner.logs("m", tail=2) == "d\ne\n"
    assert runner.logs("m", tail=100) == "a\nb\nc\nd\ne\n"
    log_file.write_text("a\nb\nc\n")  # trailing newline
    assert runner.logs("m", tail=2) == "b\nc\n"
    log_file.write_text("")  # empty
    assert runner.logs("m", tail=5) == "\n"


def test_logs_tail_on_big_file_does_not_read_it_all(tmp_path, monkeypatch):
    runner = ProcessRunner(tmp_path / "proc")
    _, log_file, _ = runner._paths("big")
    lines = [f"line {i}" for i in range(100000)]
    log_file.write_text("\n".join(lines) + "\n")
    file_size = log_file.stat().st_size

    real_open = builtins.open
    read_bytes = [0]

    class TrackingFile:
        def __init__(self, f):
            self._f = f

        def read(self, *a):
            data = self._f.read(*a)
            read_bytes[0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()
            return False

    def tracking_open(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        if "b" in mode and str(path) == str(log_file):
            return TrackingFile(f)
        return f

    monkeypatch.setattr(builtins, "open", tracking_open)
    assert runner.logs("big", tail=10) == "\n".join(lines[-10:]) + "\n"
    assert read_bytes[0] < file_size  # seek-tail, not a full read


def test_rotation_copytruncate_keeps_the_append_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(processes_mod, "LOG_MAX_BYTES", 100)
    runner = ProcessRunner(tmp_path / "proc")
    _, log_file, _ = runner._paths("rot")
    # Simulate the running child: an open append handle, exactly as start() creates it.
    with open(log_file, "ab") as writer:
        writer.write(b"x" * 150)
        writer.flush()
        runner._maybe_rotate_log(log_file)
        assert log_file.with_suffix(".log.1").read_bytes() == b"x" * 150
        assert log_file.stat().st_size == 0
        # The still-open append writer continues at the new end instead of a stale offset (no hole).
        writer.write(b"new\n")
        writer.flush()
    assert log_file.read_bytes() == b"new\n"


def test_rotation_replaces_the_previous_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(processes_mod, "LOG_MAX_BYTES", 100)
    runner = ProcessRunner(tmp_path / "proc")
    _, log_file, _ = runner._paths("rot2")
    log_file.with_suffix(".log.1").write_bytes(b"old generation")
    log_file.write_bytes(b"y" * 150)
    runner._maybe_rotate_log(log_file)
    assert log_file.with_suffix(".log.1").read_bytes() == b"y" * 150
    assert log_file.stat().st_size == 0


def test_rotation_leaves_a_small_log_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(processes_mod, "LOG_MAX_BYTES", 100)
    runner = ProcessRunner(tmp_path / "proc")
    _, log_file, _ = runner._paths("small")
    log_file.write_bytes(b"tiny")
    runner._maybe_rotate_log(log_file)
    assert log_file.read_bytes() == b"tiny"
    assert not log_file.with_suffix(".log.1").exists()


def test_status_triggers_rotation_of_a_running_log(tmp_path, monkeypatch):
    monkeypatch.setattr(processes_mod, "LOG_MAX_BYTES", 100)
    runner = ProcessRunner(tmp_path / "proc")
    name = "live"
    # Point the state at this (alive) test process so status() takes the running branch.
    state = {"name": name, "pid": os.getpid(), "create_time": psutil.Process(os.getpid()).create_time(),
             "argv": ["x"], "started_at": time.time(), "stopped": False}
    runner._paths(name)[0].write_text(json.dumps(state))
    _, log_file, _ = runner._paths(name)
    log_file.write_bytes(b"q" * 200)
    assert runner.status(name)["state"] == "running"
    assert log_file.with_suffix(".log.1").read_bytes() == b"q" * 200
    assert log_file.stat().st_size == 0
