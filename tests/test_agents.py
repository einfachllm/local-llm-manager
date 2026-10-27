"""Agent recipes (kind = "agent"): stored apart from models, left alone by "switch", checked for secrets
and for stray copies of their command."""

import asyncio
import subprocess
import sys
import time

from conftest import recipe_body

from local_llm_manager import executor as executor_mod
from local_llm_manager.db import Database
from local_llm_manager.processes import ProcessRunner


def create(client, **kw):
    r = client.post("/api/recipes", json=recipe_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def agent(client, **kw):
    return create(client, **{"name": "Gateway", "kind": "agent", "framework": "custom", "image": "hermes",
                             "command": "gateway run", "args": "", "gpus": "", "health_path": "",
                             "host_port": 8199, "env": "TELEGRAM_BOT_TOKEN\nHERMES_UID=1000", **kw})


def test_kind_defaults_to_model_and_is_validated(client):
    assert create(client)["kind"] == "model"
    assert agent(client)["kind"] == "agent"
    assert client.post("/api/recipes", json=recipe_body(name="X", kind="robot")).status_code == 422
    assert client.get("/api/meta").json()["kinds"] == {"model": "Model", "agent": "Agent"}


def test_kind_column_added_to_old_databases(tmp_path):
    path = tmp_path / "old.db"
    Database(path)._conn.execute("ALTER TABLE recipes DROP COLUMN kind")
    db = Database(path)
    rid = db.create_recipe({**recipe_body(), "container_name": "c", "host_id": None})
    assert db.get_recipe(rid)["kind"] == "model"


def test_switch_keeps_agents_running_and_refuses_an_agent(client, fake_docker, monkeypatch):
    monkeypatch.setattr(executor_mod.sys, "platform", "linux")
    a = create(client, name="A", host_port=8101)
    b = create(client, name="B", host_port=8102)
    g = agent(client)
    client.post(f"/api/recipes/{a['id']}/start")
    client.post(f"/api/recipes/{g['id']}/start")
    assert client.post(f"/api/recipes/{b['id']}/switch").status_code == 200
    statuses = {r["name"]: r["status"] for r in client.get("/api/recipes").json()}
    assert statuses == {"A": "stopped", "B": "ready", "Gateway": "ready"}

    r = client.post(f"/api/recipes/{g['id']}/switch")
    assert r.status_code == 409 and "agent" in r.json()["detail"]
    # "stop all" still stops agents too (e.g. a nightly schedule).
    assert client.post("/api/system/stop-all").status_code == 200
    assert {r["status"] for r in client.get("/api/recipes").json()} == {"stopped"}


def test_agent_reports_secret_names_but_never_values(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    g = agent(client, env="TELEGRAM_BOT_TOKEN\nMISSING_KEY\nHERMES_UID=1000")
    st = client.get(f"/api/recipes/{g['id']}").json()
    assert st["secrets"] == {"TELEGRAM_BOT_TOKEN": True, "MISSING_KEY": False}
    assert "123:secret" not in client.get("/api/recipes").text
    # Models get no agent checks.
    assert "secrets" not in client.get(f"/api/recipes/{create(client)['id']}").json()


def test_duplicates_finds_copies_started_outside_the_manager(tmp_path):
    runner = ProcessRunner(tmp_path / "proc")
    # A script, like a real agent (`hermes gateway run`): its name stays in the command line, whereas
    # the interpreter itself may show up under another name (macOS framework Python runs `Python`).
    gateway = tmp_path / "fake-gateway"
    gateway.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    gateway.chmod(0o755)
    argv = [str(gateway), "run", f"agent-dup-{time.time_ns()}"]
    runner.start("gw", argv, env={"PATH": "/usr/bin:/bin"})
    stray = subprocess.Popen(argv)
    try:
        time.sleep(0.3)
        assert runner.duplicates("gw", argv) == [stray.pid]
        stray.kill()
        stray.wait()
        assert runner.duplicates("gw", argv) == []
    finally:
        stray.kill()
        asyncio.run(runner.stop("gw", timeout=2))
