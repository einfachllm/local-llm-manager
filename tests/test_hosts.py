import httpx
import pytest
from conftest import FakeDocker, recipe_body
from fastapi.testclient import TestClient

from local_llm_manager import config
from local_llm_manager import executor as executor_mod
from local_llm_manager.agent import create_agent_app
from local_llm_manager.db import Database
from local_llm_manager.executor import LocalExecutor
from local_llm_manager.main import create_app
from local_llm_manager.processes import ProcessRunner

TOKEN = "s3cret-agent-token"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """A hub (local FakeDocker) and one agent ("mac", its own FakeDocker) wired via the agent API."""
    monkeypatch.setattr(config, "AGENT_TOKEN", TOKEN)

    async def no_local_drop():
        return "not root"

    monkeypatch.setattr(executor_mod, "try_drop_caches_locally", no_local_drop)
    monkeypatch.setattr(executor_mod.system, "read_meminfo",
                        lambda: {"total_gb": 128, "available_gb": 100, "used_gb": 28})
    hub_docker, mac_docker = FakeDocker(), FakeDocker()
    agent = create_agent_app(LocalExecutor(mac_docker, ProcessRunner(tmp_path / "agent-procs")))
    hub = create_app(Database(":memory:"), hub_docker, run_scheduler=False,
                     procs=ProcessRunner(tmp_path / "hub-procs"), remote_transport=httpx.ASGITransport(app=agent))
    with TestClient(hub, base_url="http://127.0.0.1") as client:
        mac = client.post("/api/hosts", json={"name": "mac", "url": "http://mac.local:8090", "token": TOKEN})
        assert mac.status_code == 201, mac.text
        yield client, agent, hub_docker, mac_docker, mac.json()


def test_agent_requires_token(monkeypatch):
    monkeypatch.setattr(config, "AGENT_TOKEN", TOKEN)
    agent = TestClient(create_agent_app(LocalExecutor(FakeDocker(), None)))
    assert agent.get("/agent/v1/info").status_code == 401
    assert agent.get("/agent/v1/info", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert agent.get("/agent/v1/info", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    monkeypatch.setattr(config, "AGENT_TOKEN", "")
    assert agent.get("/agent/v1/info", headers={"Authorization": "Bearer "}).status_code == 403


def test_hosts_list_never_shows_tokens(setup):
    client, _, _, _, mac = setup
    hosts = client.get("/api/hosts").json()
    assert [h["name"] for h in hosts][1] == "mac" and hosts[0]["is_local"]
    assert all("token" not in h for h in hosts) and mac["has_token"]
    assert mac["online"] and mac["system"]["memory"]["total_gb"] == 128


def test_recipe_runs_on_its_host(setup):
    client, _, hub_docker, mac_docker, mac = setup
    r = client.post("/api/recipes", json=recipe_body(host_id=mac["id"], health_path="")).json()
    assert r["host_id"] == mac["id"]
    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    assert "test-vllm" in mac_docker.containers and not hub_docker.containers
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "ready"
    assert "err" in client.get(f"/api/recipes/{r['id']}/logs").text
    assert "data: line 1" in client.get(f"/api/recipes/{r['id']}/logs/stream").text
    assert client.post(f"/api/recipes/{r['id']}/stop").status_code == 200
    assert mac_docker.containers["test-vllm"]["state"] == "exited"
    # Can't remove a host that still has recipes.
    assert client.delete(f"/api/hosts/{mac['id']}").status_code == 409


def test_switch_and_free_memory_are_per_host(setup, monkeypatch):
    monkeypatch.setattr(executor_mod.sys, "platform", "linux")  # macOS skips the cache drop
    client, _, hub_docker, mac_docker, mac = setup
    spark_a = client.post("/api/recipes", json=recipe_body(name="spark-a", host_port=8101, health_path="")).json()
    mac_a = client.post("/api/recipes", json=recipe_body(name="mac-a", host_port=8101, health_path="",
                                                         host_id=mac["id"])).json()
    mac_b = client.post("/api/recipes", json=recipe_body(name="mac-b", host_port=8102, health_path="",
                                                         host_id=mac["id"])).json()
    # Same port on different hosts is fine.
    for r in (spark_a, mac_a):
        assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    assert client.post(f"/api/recipes/{mac_b['id']}/switch").status_code == 200
    status = {r["name"]: r["status"] for r in client.get("/api/recipes").json()}
    assert status == {"spark-a": "ready", "mac-a": "stopped", "mac-b": "ready"}
    assert mac_docker.dropped == 1 and hub_docker.dropped == 0

    assert client.post(f"/api/system/free-memory?host_id={mac['id']}").status_code == 200
    status = {r["name"]: r["status"] for r in client.get("/api/recipes").json()}
    assert status == {"spark-a": "ready", "mac-a": "stopped", "mac-b": "stopped"}

    s = client.post("/api/schedules", json={"name": "night", "action": "stop_all", "cron": "0 1 * * *",
                                             "host_id": mac["id"]}).json()
    assert s["host_id"] == mac["id"]


def test_offline_or_wrong_token_host(setup):
    client, _, _, _, mac = setup
    client.put(f"/api/hosts/{mac['id']}", json={"name": "mac", "url": "http://mac.local:8090", "token": "nope"})
    host = next(h for h in client.get("/api/hosts").json() if h["id"] == mac["id"])
    assert not host["online"] and "token" in host["error"]
    r = client.post("/api/recipes", json=recipe_body(host_id=mac["id"])).json()
    assert client.get(f"/api/recipes/{r['id']}").json()["status"] == "offline"
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 502 and "token" in res.json()["detail"]


def test_host_validation(setup):
    client, _, _, _, mac = setup
    local = client.get("/api/hosts").json()[0]
    assert client.delete(f"/api/hosts/{local['id']}").status_code == 409
    assert client.post("/api/hosts", json={"name": "x", "url": "mac.local", "token": "t"}).status_code == 422
    assert client.post("/api/hosts", json={"name": "y", "url": "http://y:1"}).status_code == 422  # no token
    assert client.post("/api/hosts", json={"name": "mac", "url": "http://z:1", "token": "t"}).status_code == 409
    assert client.post("/api/recipes", json=recipe_body(host_id=999)).status_code == 404
    # Renaming the local host works; it keeps having no URL.
    assert client.put(f"/api/hosts/{local['id']}", json={"name": "spark"}).json()["name"] == "spark"
