import asyncio
import time

import httpx
import test_hosts
from conftest import FakeDocker, recipe_body

from local_llm_manager import config
from local_llm_manager import executor as executor_mod
from local_llm_manager.agent import create_agent_app
from local_llm_manager.db import Database
from local_llm_manager.executor import LocalExecutor
from local_llm_manager.manager import Manager
from local_llm_manager.processes import ProcessRunner

setup = test_hosts.setup  # the hub + one agent ("mac") fixture


class SlowDocker(FakeDocker):
    """`docker run` waits until the test lets it go - like pulling a large image."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    async def run_argv(self, argv):
        await self.gate.wait()
        return await super().run_argv(argv)


def test_a_slow_start_on_one_host_does_not_block_the_others(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENT_TOKEN", test_hosts.TOKEN)

    async def no_local_drop():
        return "not root"

    monkeypatch.setattr(executor_mod, "try_drop_caches_locally", no_local_drop)
    monkeypatch.setattr(executor_mod.system, "read_meminfo",
                        lambda: {"total_gb": 128, "available_gb": 100, "used_gb": 28})

    async def main():
        slow = SlowDocker()
        agent = create_agent_app(LocalExecutor(slow, ProcessRunner(tmp_path / "agent-procs")))
        db = Database(":memory:")
        manager = Manager(db, FakeDocker(), ProcessRunner(tmp_path / "hub-procs"), httpx.ASGITransport(app=agent))
        mac = db.create_host("mac", "http://mac.local:8090", test_hosts.TOKEN)
        far = db.create_recipe({**recipe_body(name="far", health_path=""), "container_name": "far", "host_id": mac})
        near = db.create_recipe({**recipe_body(name="near", health_path=""), "container_name": "near",
                                 "host_id": db.local_host_id()})

        pulling = asyncio.create_task(manager.start(far))
        await asyncio.sleep(0.1)
        assert not pulling.done()
        # The hub's own host answers right away, and so does stopping everything there.
        assert "near" in await asyncio.wait_for(manager.start(near), 2)
        await asyncio.wait_for(manager.stop_all(host_id=db.local_host_id()), 2)
        # A second action on the busy host waits for the first one.
        queued = asyncio.create_task(manager.stop(far))
        await asyncio.sleep(0.1)
        assert not queued.done()
        slow.gate.set()
        await asyncio.wait_for(pulling, 2)
        await asyncio.wait_for(queued, 2)

    asyncio.run(main())


def test_restart_job_runs_on_the_hub(setup):
    client, _, hub_docker, mac_docker, mac = setup
    a = client.post("/api/recipes", json=recipe_body(name="a", container_name="a", health_path="")).json()
    b = client.post("/api/recipes", json=recipe_body(name="b", container_name="b", health_path="",
                                                     host_id=mac["id"])).json()
    for r in (a, b):
        assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200

    job = client.post("/api/jobs/restart", json={"recipe_ids": [a["id"], b["id"], 999]})
    assert job.status_code == 404
    job = client.post("/api/jobs/restart", json={"recipe_ids": [a["id"], b["id"]], "title": "Copies"})
    assert job.status_code == 202
    job_id = job.json()["id"]
    deadline = time.time() + 5
    while not (job := client.get(f"/api/jobs/{job_id}").json())["done"] and time.time() < deadline:
        time.sleep(0.02)
    assert job["done"] and [i["state"] for i in job["items"]] == ["ok", "ok"]
    # When it finished: the dashboard shows recently finished jobs to a page that missed them.
    assert job["finished"] >= job["created"]
    assert ["stop", "a"] in hub_docker.calls and ["stop", "b"] in mac_docker.calls
    assert client.get("/api/jobs").json()[0]["id"] == job_id
    assert client.get("/api/jobs/nope").status_code == 404

    # A host that fails does not stop the job for the others.
    mac_docker.fail_run = True
    job_id = client.post("/api/jobs/restart", json={"recipe_ids": [b["id"], a["id"]]}).json()["id"]
    deadline = time.time() + 5
    while not (job := client.get(f"/api/jobs/{job_id}").json())["done"] and time.time() < deadline:
        time.sleep(0.02)
    states = {i["name"]: i["state"] for i in job["items"]}
    assert states == {"a": "ok", "b": "failed"}
