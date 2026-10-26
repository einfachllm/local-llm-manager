import socket
import sys
import time

from conftest import recipe_body

from llm_model_manager.processes import ProcessRunner


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
    assert cmd.startswith("GREETING=hello ") and cmd.endswith("-m http.server 9002 --bind 127.0.0.1")


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
