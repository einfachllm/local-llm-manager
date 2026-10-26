import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conftest import recipe_body

from local_llm_manager import executor as executor_mod
from local_llm_manager.recipes import build_run_argv
from local_llm_manager.scheduler import compute_next_run, local_now


def create(client, **kw):
    r = client.post("/api/recipes", json=recipe_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def test_build_run_argv():
    argv = build_run_argv({**recipe_body(), "id": 7, "container_name": "test", "gpus": "all"})
    image_at = argv.index("nvcr.io/nvidia/vllm:25.11-py3")
    assert argv[:5] == ["docker", "run", "-d", "--name", "test"]
    assert ["-p", "8100:8000"] == argv[argv.index("-p"):argv.index("-p") + 2]
    assert "HF_TOKEN" in argv and "FOO=bar" in argv and "--ipc=host" in argv[:image_at]
    assert argv[image_at + 1:] == [
        "vllm", "serve", "Qwen/Qwen3-8B", "--host", "0.0.0.0", "--port", "8000",
        "--max-model-len", "32768", "--gpu-memory-utilization", "0.4",
    ]


def test_json_args_with_braces_survive():
    argv = build_run_argv({**recipe_body(args="--override-generation-config '{\"temperature\": 0.7}'"),
                           "container_name": "t"})
    assert argv[-1] == '{"temperature": 0.7}'


def test_crud_and_container_name_derived(client):
    r = create(client, name="My Model 1")
    assert r["container_name"] == "my-model-1"
    assert client.post("/api/recipes", json=recipe_body(name="My Model 1")).status_code == 409

    upd = client.put(f"/api/recipes/{r['id']}", json=recipe_body(name="Renamed", container_name="my-model-1"))
    assert upd.status_code == 200 and upd.json()["name"] == "Renamed"

    dup = client.post(f"/api/recipes/{r['id']}/duplicate").json()
    assert dup["name"] == "Renamed (copy 1)" and dup["container_name"] == "my-model-1-copy1"

    assert client.delete(f"/api/recipes/{dup['id']}").status_code == 204
    assert len(client.get("/api/recipes").json()) == 1


def test_validation(client):
    assert client.post("/api/recipes", json=recipe_body(framework="nope")).status_code == 422
    assert client.post("/api/recipes", json=recipe_body(container_name="bad name")).status_code == 422
    assert client.post("/api/recipes", json=recipe_body(host_port=70000)).status_code == 422


def test_preview(client):
    cmd = client.post("/api/recipes/preview", json=recipe_body()).json()["command"]
    assert cmd.startswith("docker run -d --name test-vllm")
    assert "vllm serve Qwen/Qwen3-8B" in cmd


def test_start_stop_restart(client, fake_docker):
    r = create(client)
    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"
    # Starting again is a no-op.
    assert "already running" in client.post(f"/api/recipes/{r['id']}/start").json()["message"]
    assert len([c for c in fake_docker.calls if c[0] == "docker"]) == 1

    # Can't delete while running.
    assert client.delete(f"/api/recipes/{r['id']}").status_code == 409

    # Edit + restart re-creates the container with the new args.
    client.put(f"/api/recipes/{r['id']}", json=recipe_body(args="--max-model-len 1024"))
    assert client.post(f"/api/recipes/{r['id']}/restart").status_code == 200
    last_run = [c for c in fake_docker.calls if c[0] == "docker"][-1]
    assert last_run[-2:] == ["--max-model-len", "1024"]
    assert ["rm", "test-vllm"] in fake_docker.calls

    assert client.post(f"/api/recipes/{r['id']}/stop").status_code == 200
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "stopped"


def test_port_conflict_and_memory_check(client, monkeypatch):
    a = create(client, name="A")
    b = create(client, name="B")
    client.post(f"/api/recipes/{a['id']}/start")
    res = client.post(f"/api/recipes/{b['id']}/start")
    assert res.status_code == 409 and "Port 8100" in res.json()["detail"]

    c = create(client, name="C", host_port=8200, est_memory_gb=500)
    res = client.post(f"/api/recipes/{c['id']}/start")
    assert res.status_code == 409 and "memory" in res.json()["detail"]
    assert client.post(f"/api/recipes/{c['id']}/start?force=true").status_code == 200


def test_docker_failure_reported(client, fake_docker):
    r = create(client)
    fake_docker.fail_run = True
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 500 and "boom" in res.json()["detail"]
    events = client.get("/api/events").json()
    assert events[0]["ok"] == 0 and "boom" in events[0]["message"]


def test_switch_stops_others_and_drops_caches(client, fake_docker):
    a = create(client, name="A", host_port=8101)
    b = create(client, name="B", host_port=8102)
    client.post(f"/api/recipes/{a['id']}/start")
    assert client.post(f"/api/recipes/{b['id']}/switch").status_code == 200
    statuses = {r["name"]: r["status"] for r in client.get("/api/recipes").json()}
    assert statuses == {"A": "stopped", "B": "ready"}
    assert fake_docker.dropped == 1


def test_running_recipe_reports_live_server_metrics(client, fake_docker, monkeypatch):
    """/metrics wins over the log line, and token rates follow the counters between polls."""
    counters = {"gen": 1000}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (f"vllm:num_requests_running 1\nvllm:kv_cache_usage_perc 0.25\n"
                    f"vllm:prompt_tokens_total 0\nvllm:generation_tokens_total {counters['gen']}\n").encode()
            self.send_response(200 if self.path == "/metrics" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    clock = [100.0]
    try:
        fake_docker.log_stats = "INFO Engine 000: Avg prompt throughput: 1.0 tokens/s, Avg generation throughput: 2.0 tokens/s"
        r = create(client, host_port=server.server_address[1])
        client.app.state.manager.local.rates.clock = lambda: clock[0]
        assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
        first = client.get(f"/api/recipes/{r['id']}").json()["metrics"]
        assert first == {"running": 1, "kv_cache": 25.0, "source": "metrics"}
        counters["gen"] += 250
        clock[0] += 5
        assert client.get(f"/api/recipes/{r['id']}").json()["metrics"]["gen_tps"] == 50.0
    finally:
        server.shutdown()


def test_running_recipe_reports_log_metrics(client, fake_docker):
    fake_docker.log_stats = (
        "(APIServer pid=1) INFO 10-25 18:34:51 [loggers.py:315] Engine 000: "
        "Avg prompt throughput: 4897.8 tokens/s, Avg generation throughput: 18.2 tokens/s, "
        "Running: 2 reqs, Waiting: 1 reqs, GPU KV cache usage: 11.2%, Prefix cache hit rate: 46.5%")
    r = create(client)
    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    row = client.get(f"/api/recipes/{r['id']}").json()
    assert row["status"] == "ready"
    assert row["metrics"] == {"prompt_tps": 4897.8, "gen_tps": 18.2, "running": 2,
                              "waiting": 1, "kv_cache": 11.2, "prefix_hit": 46.5, "source": "log"}
    # Stopped recipes drop the metrics again.
    client.post(f"/api/recipes/{r['id']}/stop")
    assert "metrics" not in client.get(f"/api/recipes/{r['id']}").json()


def test_logs(client):
    r = create(client)
    client.post(f"/api/recipes/{r['id']}/start")
    text = client.get(f"/api/recipes/{r['id']}/logs").text
    assert text.splitlines() == ["2026-01-01T00:00:00Z err", "2026-01-01T00:00:01Z out"]
    stream = client.get(f"/api/recipes/{r['id']}/logs/stream").text
    assert "data: line 1" in stream and "event: end" in stream


def test_failed_exit_detection():
    assert executor_mod._failed_exit("Exited (1) 2 minutes ago")
    assert not executor_mod._failed_exit("Exited (137) 2 minutes ago")
    assert not executor_mod._failed_exit("Up 3 hours")


def test_schedule_crud_and_validation(client):
    r = create(client)
    bad = client.post("/api/schedules", json={"name": "x", "action": "start", "recipe_id": r["id"], "cron": "nope"})
    assert bad.status_code == 422
    both = client.post("/api/schedules", json={"name": "x", "action": "stop_all", "cron": "0 1 * * *",
                                                "run_at": "2030-01-01T00:00"})
    assert both.status_code == 422
    missing = client.post("/api/schedules", json={"name": "x", "action": "start", "cron": "0 1 * * *"})
    assert missing.status_code == 422

    s = client.post("/api/schedules", json={"name": "nightly", "action": "switch", "recipe_id": r["id"],
                                             "cron": "0 22 * * 1-5"}).json()
    assert s["next_run"] and "T22:00:00" in s["next_run"]

    off = client.put(f"/api/schedules/{s['id']}", json={**s, "enabled": False}).json()
    assert off["enabled"] == 0 and off["next_run"] is None

    # Deleting the recipe cascades to its schedules.
    client.delete(f"/api/recipes/{r['id']}")
    assert client.get("/api/schedules").json() == []


def test_schedule_run_now(client):
    r = create(client)
    s = client.post("/api/schedules", json={"name": "go", "action": "start", "recipe_id": r["id"],
                                             "cron": "0 3 * * *"}).json()
    assert client.post(f"/api/schedules/{s['id']}/run").status_code == 200
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"
    s = client.get("/api/schedules").json()[0]
    assert s["last_ok"] == 1


def test_scheduler_tick_fires_due_once_schedule(client):
    r = create(client)
    run_at = (local_now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    client.post("/api/schedules", json={"name": "once", "action": "start", "recipe_id": r["id"], "run_at": run_at})
    scheduler = client.app.state.scheduler
    client.portal.call(scheduler.tick)
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"
    s = client.get("/api/schedules").json()[0]
    assert s["enabled"] == 0 and s["last_ok"] == 1


def test_compute_next_run_cron():
    now = local_now().replace(hour=10, minute=0, second=0, microsecond=0)
    nxt = compute_next_run({"cron": "30 12 * * *"}, now)
    assert nxt.startswith(now.date().isoformat()) and "T12:30:00" in nxt


def test_tags_roundtrip_and_duplicate(client):
    r = create(client, tags=[" Qwen3.8 Flash ", "fp4", "qwen3.8   flash", "", "long,ctx"])
    assert r["tags"] == ["Qwen3.8 Flash", "fp4", "long ctx"]
    assert client.get("/api/recipes").json()[0]["tags"] == ["Qwen3.8 Flash", "fp4", "long ctx"]

    upd = client.put(f"/api/recipes/{r['id']}", json=recipe_body(tags="a, b ,a")).json()
    assert upd["tags"] == ["a", "b"]

    dup = client.post(f"/api/recipes/{r['id']}/duplicate").json()
    assert dup["tags"] == ["a", "b"]
    assert create(client, name="untagged", host_port=8300)["tags"] == []


def test_tags_column_added_to_existing_db(tmp_path):
    import sqlite3

    from local_llm_manager.db import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '', framework TEXT NOT NULL, image TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '', container_name TEXT NOT NULL UNIQUE, host_port INTEGER NOT NULL,
        container_port INTEGER NOT NULL, command TEXT NOT NULL DEFAULT '', args TEXT NOT NULL DEFAULT '',
        env TEXT NOT NULL DEFAULT '', volumes TEXT NOT NULL DEFAULT '', docker_args TEXT NOT NULL DEFAULT '',
        gpus TEXT NOT NULL DEFAULT 'all', health_path TEXT NOT NULL DEFAULT '/health', est_memory_gb REAL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""INSERT INTO recipes (name, framework, image, container_name, host_port, container_port,
        created_at, updated_at) VALUES ('old', 'vllm', 'img', 'old', 8000, 8000, 'x', 'x')""")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.list_recipes()[0]["tags"] == []
    db.update_recipe(1, {**db.get_recipe(1), "tags": ["kept"]})
    assert Database(path).get_recipe(1)["tags"] == ["kept"]


def test_host_network_keeps_single_port(client):
    r = create(client, docker_args="--network host --ipc host", host_port=8082, container_port=8081)
    assert r["container_port"] == 8082
    cmd = client.post("/api/recipes/preview", json=recipe_body(docker_args="--net=host", host_port=9000)).json()["command"]
    assert "--port 9000" in cmd and " -p " not in cmd
    assert client.post("/api/recipes", json=recipe_body(name="q", docker_args="--x 'oops")).status_code == 422


def test_card_color(client):
    r = create(client, color="Green")
    assert r["color"] == "green"
    assert create(client, name="plain", host_port=8301)["color"] == ""
    assert client.post("/api/recipes", json=recipe_body(name="x", color="chartreuse")).status_code == 422
    assert client.post(f"/api/recipes/{r['id']}/duplicate").json()["color"] == "green"
    assert "black" in client.get("/api/meta").json()["colors"]


def test_start_refuses_port_used_by_foreign_program(client, fake_docker):
    import socket

    with socket.socket() as srv:  # binding just picks a free port number
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        fake_docker.ports_in_use.add(port)
        r = create(client, host_port=port)
        res = client.post(f"/api/recipes/{r['id']}/start")
        assert res.status_code == 409 and f"Port {port}" in res.json()["detail"] and "did not start" in res.json()["detail"]
        assert not fake_docker.containers
    # Once the other program is gone, it starts.
    fake_docker.ports_in_use.discard(port)
    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
