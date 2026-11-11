import asyncio
import sys
import time

import httpx
import pytest
import test_hosts
from conftest import FakeDocker
from fastapi.testclient import TestClient

from local_llm_manager import config, transfers
from local_llm_manager import executor as executor_mod
from local_llm_manager.agent import create_agent_app
from local_llm_manager.db import Database
from local_llm_manager.executor import LocalExecutor
from local_llm_manager.main import create_app
from local_llm_manager.manager import ActionError, Manager
from local_llm_manager.processes import ProcessRunner

setup = test_hosts.setup  # the hub + one agent ("mac") fixture


class AcrossLoops(httpx.AsyncBaseTransport):
    """Routes hub→agent calls through the agent's own TestClient portal, so the agent's background
    tasks run on its own event loop and survive a hub restart (like a real separate machine)."""

    def __init__(self, agent_client: TestClient):
        self.ac = agent_client
        self.inner = httpx.ASGITransport(app=agent_client.app)

    async def handle_async_request(self, request):
        # portal.call runs the coroutine on the agent's loop and returns its result (already awaited)
        return self.ac.portal.call(self.inner.handle_async_request, request)


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    """A hub on a file database and an agent on its own loop, with a gated `hf download`."""
    monkeypatch.setattr(config, "AGENT_TOKEN", test_hosts.TOKEN)
    monkeypatch.setattr(config, "MODEL_DIRS", [str(tmp_path / "models")])

    async def no_local_drop():
        return "not root"

    monkeypatch.setattr(executor_mod, "try_drop_caches_locally", no_local_drop)
    monkeypatch.setattr(executor_mod.system, "read_meminfo",
                        lambda: {"total_gb": 128, "available_gb": 100, "used_gb": 28})

    def fake_argv(repo, target):
        code = ("import sys, pathlib; t = pathlib.Path(sys.argv[2]); "
                "(t / 'model.safetensors').write_bytes(b'x' * 1000)")
        return [sys.executable, "-c", code, repo, target]

    monkeypatch.setattr(transfers, "download_argv", fake_argv)
    gate = asyncio.Event()
    original = transfers.Tasks._download

    async def gated(self, task, repo, target):
        await gate.wait()
        return await original(self, task, repo, target)

    monkeypatch.setattr(transfers.Tasks, "_download", gated)
    agent = create_agent_app(LocalExecutor(FakeDocker(), ProcessRunner(tmp_path / "agent-procs")))
    with TestClient(agent, base_url="http://agent.local:8090") as ac:
        yield tmp_path / "hub.db", AcrossLoops(ac), ac, gate


def _hub(db_path, transport, tmp_path, name):
    app = create_app(Database(db_path), FakeDocker(), run_scheduler=False,
                     procs=ProcessRunner(tmp_path / name), remote_transport=transport)
    return app


def _wait_job(client, job_id, timeout=5):
    deadline = time.time() + timeout
    while not (job := client.get(f"/api/jobs/{job_id}").json())["done"] and time.time() < deadline:
        time.sleep(0.02)
    assert job["done"], job
    return job


@pytest.fixture
def hub(setup, monkeypatch, tmp_path):
    client, _, hub_docker, mac_docker, mac = setup
    client.app.state.manager.transfer_poll = 0.02
    monkeypatch.setattr(config, "MODEL_DIRS", [str(tmp_path / "models")])

    # Instead of `hf download`: write a small file into --local-dir (fails for repos named "org/broken").
    def fake_argv(repo, target):
        code = ("import sys, pathlib; repo, t = sys.argv[1], pathlib.Path(sys.argv[2]); "
                "sys.exit('401 gated repo') if repo.endswith('broken') else "
                "(t / 'model.safetensors').write_bytes(b'x' * 1000)")
        return [sys.executable, "-c", code, repo, target]

    monkeypatch.setattr(transfers, "download_argv", fake_argv)
    local = client.get("/api/hosts").json()[0]["id"]
    return client, hub_docker, mac_docker, local, mac["id"]


def wait(client, job_id):
    deadline = time.time() + 5
    while not (job := client.get(f"/api/jobs/{job_id}").json())["done"] and time.time() < deadline:
        time.sleep(0.02)
    assert job["done"], job
    return {i["host_id"]: i for i in job["items"]}


def test_pull_on_several_hosts(hub):
    client, hub_docker, mac_docker, local, mac = hub
    assert client.post("/api/jobs/pull", json={"image": "-rm", "host_ids": [local]}).status_code == 422
    assert client.post("/api/jobs/pull", json={"image": "x:1", "host_ids": [999]}).status_code == 404

    mac_docker.fail_pull = True
    res = client.post("/api/jobs/pull", json={"image": "vllm/vllm-openai:v1", "host_ids": [local, mac]})
    assert res.status_code == 202 and res.json()["kind"] == "pull"
    items = wait(client, res.json()["id"])
    assert items[local]["state"] == "ok" and items[local]["message"] == "Pulled vllm/vllm-openai:v1"
    assert items[mac]["state"] == "failed" and "manifest unknown" in items[mac]["message"]
    assert ["pull", "vllm/vllm-openai:v1"] in hub_docker.calls and ["pull", "vllm/vllm-openai:v1"] in mac_docker.calls


def test_download_on_several_hosts_registers_the_weights(hub, tmp_path):
    client, _, _, local, mac = hub
    assert client.post("/api/jobs/download", json={"repo": "not-a-repo", "host_ids": [local]}).status_code == 422
    assert client.post("/api/jobs/download", json={"repo": "a/b", "name": "../x", "host_ids": [local]}).status_code == 422

    res = client.post("/api/jobs/download", json={"repo": "Qwen/Qwen3-8B", "host_ids": [local, mac], "once": False})
    items = wait(client, res.json()["id"])
    assert [i["state"] for i in items.values()] == ["ok", "ok"], items
    assert (tmp_path / "models" / "Qwen3-8B" / "model.safetensors").exists()
    (w,) = client.get("/api/weights?check=false").json()["weights"]
    assert w["name"] == "Qwen3-8B" and w["source"] == "Qwen/Qwen3-8B"
    assert w["locations"] == {str(local): str(tmp_path / "models" / "Qwen3-8B"),
                              str(mac): str(tmp_path / "models" / "Qwen3-8B")}

    # Again into an existing entry under another folder name; a failing download reports why.
    res = client.post("/api/jobs/download", json={"repo": "org/broken", "host_ids": [mac], "weight_id": w["id"]})
    (item,) = wait(client, res.json()["id"]).values()
    assert item["state"] == "failed" and "gated" in item["message"]
    assert client.post("/api/jobs/download", json={"repo": "a/b", "host_ids": [mac], "weight_id": 999}).status_code == 404


def test_missing_hf_cli_is_reported(monkeypatch):
    monkeypatch.setattr(transfers.shutil, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="huggingface_hub"):
        transfers.download_argv("a/b", "/tmp/x")


def test_docker_pull_streams_progress(tmp_path):
    import asyncio

    from local_llm_manager.docker import Docker

    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\necho \"$2: Pulling fs layer\"\necho \"$2: Pull complete\"\n"
                    "[ \"$2\" = bad:1 ] && { echo 'manifest unknown' >&2; exit 1; }\nexit 0\n")
    fake.chmod(0o755)
    seen = []
    ok = asyncio.run(Docker(str(fake)).pull("good:1", seen.append))
    assert ok.ok and seen == ["good:1: Pulling fs layer", "good:1: Pull complete"]
    bad = asyncio.run(Docker(str(fake)).pull("bad:1"))
    assert not bad.ok and "manifest unknown" in bad.error
    assert asyncio.run(Docker(str(tmp_path / "nope")).pull("x:1")).returncode == 127


# ---- jobs survive a hub restart (FLEET-08) --------------------------------------------------------


def test_job_reattaches_after_hub_restart(fleet, tmp_path):
    """The hub restarts while a download runs: the new manager picks the job up from the database,
    follows the agent's still-running task and registers the weights when it finishes."""
    db_path, transport, ac, gate = fleet
    with TestClient(_hub(db_path, transport, tmp_path, "hub1"), base_url="http://127.0.0.1") as c1:
        mac = c1.post("/api/hosts", json={"name": "mac", "url": "http://mac.local:8090",
                                          "token": test_hosts.TOKEN}).json()
        c1.app.state.manager.transfer_poll = 0.02
        job = c1.post("/api/jobs/download", json={"repo": "org/slow", "host_ids": [mac["id"]],
                                                  "once": False}).json()
        deadline = time.time() + 5
        while time.time() < deadline:
            item = c1.get(f"/api/jobs/{job['id']}").json()["items"][0]
            if item["state"] == "running" and item.get("task_id"):
                break
            time.sleep(0.02)
        assert item.get("task_id"), item
    # hub1 is gone; the agent's download is still waiting at the gate on its own loop
    with TestClient(_hub(db_path, transport, tmp_path, "hub2"), base_url="http://127.0.0.1") as c2:
        c2.app.state.manager.transfer_poll = 0.02
        c2.portal.call(c2.app.state.manager.resume_jobs)  # like the lifespan does on a real start
        assert not c2.get(f"/api/jobs/{job['id']}").json()["done"]
        ac.portal.call(gate.set)
        done = _wait_job(c2, job["id"])
        assert done["items"][0]["state"] == "ok", done
        (w,) = c2.get("/api/weights?check=false").json()["weights"]
        assert w["name"] == "slow" and str(mac["id"]) in w["locations"]


def test_transfer_restarts_when_the_agent_lost_it(fleet, tmp_path):
    """The agent restarts and loses its task (404): the hub starts the transfer again there instead
    of failing the job - the download picks up what is already on disk."""
    db_path, transport, ac, gate = fleet
    with TestClient(_hub(db_path, transport, tmp_path, "hub1"), base_url="http://127.0.0.1") as c1:
        mac = c1.post("/api/hosts", json={"name": "mac", "url": "http://mac.local:8090",
                                          "token": test_hosts.TOKEN}).json()
        c1.app.state.manager.transfer_poll = 0.02
        job = c1.post("/api/jobs/download", json={"repo": "org/slow", "host_ids": [mac["id"]],
                                                  "once": False}).json()
        deadline = time.time() + 5
        while time.time() < deadline:
            item = c1.get(f"/api/jobs/{job['id']}").json()["items"][0]
            if item["state"] == "running" and item.get("task_id"):
                break
            time.sleep(0.02)
        assert item.get("task_id"), item

        def agent_restart():
            tasks = ac.app.state.executor.transfers
            for t in list(tasks._running):
                t.cancel()
            tasks.tasks.clear()

        ac.portal.call(agent_restart)
        ac.portal.call(gate.set)
        done = _wait_job(c1, job["id"])
        assert done["items"][0]["state"] == "ok", done
        assert done["items"][0].get("restarts", 0) >= 1
        assert (tmp_path / "models" / "slow" / "model.safetensors").exists()


def test_follow_gives_up_on_a_stalled_transfer(tmp_path):
    """A transfer whose progress never changes is abandoned after the stall window (CORE-15)."""
    async def main():
        m = Manager(Database(":memory:"), FakeDocker(), ProcessRunner(tmp_path / "p"))
        m.transfer_poll = 0.01
        m.transfer_stall = 0.05

        class Stuck:
            async def transfer(self, task_id):
                return {"id": task_id, "state": "running", "progress": "1.0 / 2.0 GB", "message": ""}

        item = {"state": "waiting", "message": "", "progress": ""}
        task = {"id": "t", "state": "running", "progress": "1.0 / 2.0 GB"}
        with pytest.raises(ActionError, match="No progress"):
            await asyncio.wait_for(m._follow(Stuck(), task, item), 5)

    asyncio.run(main())


def test_job_eviction_spares_running_jobs(tmp_path):
    """More than 20 jobs: finished ones are dropped for the dashboard, running ones never (CORE-15)."""
    async def main():
        db = Database(tmp_path / "m.db")
        m = Manager(db, FakeDocker(), ProcessRunner(tmp_path / "p"))
        gate = asyncio.Event()

        async def slow(item):
            await gate.wait()
            item["state"] = "ok"

        async def quick(item):
            item["state"] = "ok"

        def items():
            return [{"host_id": 1, "name": "", "state": "waiting", "message": "", "progress": ""}]

        async def wait_until(cond, timeout=5.0):
            """Poll instead of sleeping a fixed time: a loaded CI machine may need longer."""
            deadline = asyncio.get_running_loop().time() + timeout
            while not cond():
                assert asyncio.get_running_loop().time() < deadline, "timed out waiting for the jobs"
                await asyncio.sleep(0.01)

        running = [m._new_job("pull", f"r{i}", items(), slow) for i in range(3)]
        for i in range(25):
            m._new_job("pull", f"d{i}", items(), quick)
        running_ids = {j["id"] for j in running}
        # Every job that is not one of the gated ones has finished (and older ones were evicted).
        await wait_until(lambda: all(j["done"] for j in m.jobs.values() if j["id"] not in running_ids))
        assert all(j["id"] in m.jobs and not m.jobs[j["id"]]["done"] for j in running)
        assert len(m.jobs) <= 23  # 3 running + at most 20 finished
        assert {j["id"] for j in m.jobs.values()} == {j["id"] for j in db.load_jobs()}
        gate.set()
        await wait_until(lambda: all(m.jobs[j["id"]]["done"] for j in running))
        assert all(m.jobs[j["id"]]["done"] for j in running)

    asyncio.run(main())
