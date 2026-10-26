import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DGXM_SEED", "0")

from llm_model_manager import executor as executor_mod  # noqa: E402
from llm_model_manager.db import Database  # noqa: E402
from llm_model_manager.docker import CommandResult  # noqa: E402
from llm_model_manager.main import create_app  # noqa: E402
from llm_model_manager.processes import ProcessRunner  # noqa: E402


class FakeDocker:
    """In-memory stand-in for the docker CLI."""

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.fail_run = False
        self.dropped = 0
        # Ports the fake executor reports as already listening (foreign programs).
        self.ports_in_use: set[int] = set()
        # Extra log line the fake prints, e.g. a vLLM engine stats line (see test_api metrics test).
        self.log_stats = ""

    async def run_argv(self, argv):
        self.calls.append(argv)
        if self.fail_run:
            return CommandResult(125, "", "boom")
        name = argv[argv.index("--name") + 1]
        self.containers[name] = {"id": "abc123", "state": "running", "status": "Up 1 second", "image": ""}
        return CommandResult(0, "abc123def456\n", "")

    async def list_containers(self):
        return {k: dict(v) for k, v in self.containers.items()}

    async def stop(self, name, timeout=30):
        self.calls.append(["stop", name])
        self.containers[name].update(state="exited", status="Exited (0) 1 second ago")
        return CommandResult(0, name, "")

    async def remove(self, name):
        self.calls.append(["rm", name])
        self.containers.pop(name, None)
        return CommandResult(0, name, "")

    async def logs(self, name, tail=500):
        out = "2026-01-01T00:00:01Z out\n" + (f"{self.log_stats}\n" if self.log_stats else "")
        return CommandResult(0, out, "2026-01-01T00:00:00Z err\n")

    async def follow_logs(self, name, tail=200):
        for line in ("line 1", "line 2"):
            yield line

    async def drop_caches(self, image=""):
        self.dropped += 1
        return CommandResult(0, "", "")


@pytest.fixture
def fake_docker():
    return FakeDocker()


@pytest.fixture
def client(fake_docker, monkeypatch, tmp_path):
    async def no_local_drop():
        return "not root"

    monkeypatch.setattr(executor_mod, "try_drop_caches_locally", no_local_drop)
    # Plenty of free memory unless a test says otherwise.
    monkeypatch.setattr(executor_mod.system, "read_meminfo",
                        lambda: {"total_gb": 128, "available_gb": 100, "used_gb": 28})
    # Ports on the dev/CI machine must not influence start decisions (e.g. Vault on 8200);
    # a test simulates a foreign listener by adding the port to fake_docker.ports_in_use.
    monkeypatch.setattr(executor_mod, "port_in_use", lambda port: port in fake_docker.ports_in_use)
    app = create_app(Database(":memory:"), fake_docker, run_scheduler=False,
                     procs=ProcessRunner(tmp_path / "processes"))
    with TestClient(app) as c:
        yield c


def recipe_body(**overrides):
    body = {
        "name": "Test vLLM",
        "framework": "vllm",
        "image": "nvcr.io/nvidia/vllm:25.11-py3",
        "model": "Qwen/Qwen3-8B",
        "host_port": 8100,
        "container_port": 8000,
        "command": "vllm serve {model} --host 0.0.0.0 --port {port}",
        "args": "--max-model-len 32768\n# comment\n--gpu-memory-utilization 0.4",
        "env": "HF_TOKEN\nFOO=bar",
        "volumes": "/data/hf:/root/.cache/huggingface",
        "docker_args": "--ipc=host",
        "health_path": "",
        "est_memory_gb": 20,
    }
    body.update(overrides)
    return body
