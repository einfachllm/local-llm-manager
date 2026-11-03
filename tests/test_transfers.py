import sys
import time

import pytest
import test_hosts

from local_llm_manager import config, transfers

setup = test_hosts.setup  # the hub + one agent ("mac") fixture


@pytest.fixture
def hub(setup, monkeypatch, tmp_path):
    client, _, hub_docker, mac_docker, mac = setup
    client.app.state.manager.transfer_poll = 0.02
    monkeypatch.setattr(config, "MODEL_DIRS", [str(tmp_path / "models")])

    # Instead of `hf download`: write a small file into --local-dir (fails for repos named "org/broken").
    # With --include, only the files of a pretend GGUF repo that match are written.
    def fake_argv(repo, target, include=None):
        code = ("import sys, pathlib, fnmatch; repo, t, inc = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3:]; "
                "sys.exit('401 gated repo') if repo.endswith('broken') else "
                "[(t / f).write_bytes(b'x' * 1000) for f in (['model.safetensors'] if not inc else "
                "[f for f in ('m-Q4_K_M.gguf', 'm-Q8_0.gguf', 'mmproj-F16.gguf') "
                "if any(fnmatch.fnmatch(f, p) for p in inc)])]")
        return [sys.executable, "-c", code, repo, target, *(include or [])]

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


def test_download_only_some_files(hub, tmp_path, monkeypatch):
    client, _, _, local, mac = hub
    bad = client.post("/api/jobs/download", json={"repo": "a/b-GGUF", "host_ids": [local], "include": ["--force"]})
    assert bad.status_code == 422 and "file pattern" in bad.text
    res = client.post("/api/jobs/download", json={"repo": "org/m-GGUF", "host_ids": [local, mac], "once": False,
                                                  "include": ["*Q4_K_M*.gguf", "mmproj-*"]})
    assert "(*Q4_K_M*.gguf mmproj-*)" in res.json()["title"]
    items = wait(client, res.json()["id"])
    assert [i["state"] for i in items.values()] == ["ok", "ok"], items
    assert sorted(f.name for f in (tmp_path / "models" / "m-GGUF").iterdir()) == ["m-Q4_K_M.gguf", "mmproj-F16.gguf"]

    # A pattern that matches nothing is a failed download, not an empty model.
    res = client.post("/api/jobs/download", json={"repo": "org/x-GGUF", "host_ids": [local], "include": ["*Q3_K*"]})
    (item,) = wait(client, res.json()["id"]).values()
    assert item["state"] == "failed" and "No file in org/x-GGUF matches *Q3_K*" in item["message"]

    # An agent without the feature would ignore the patterns and fetch the whole repo: it is refused.
    from local_llm_manager import executor
    monkeypatch.setattr(executor, "FEATURES", [])
    res = client.post("/api/jobs/download", json={"repo": "org/y-GGUF", "host_ids": [mac], "include": ["*Q8_0*"]})
    (item,) = wait(client, res.json()["id"]).values()
    assert item["state"] == "failed" and "too old to download only some files" in item["message"]
    assert not (tmp_path / "models" / "y-GGUF").exists()


def test_download_argv_and_include_patterns(monkeypatch):
    monkeypatch.setattr(transfers.shutil, "which", lambda b: b == "hf")
    assert transfers.download_argv("a/b", "/m/b") == ["hf", "download", "a/b", "--local-dir", "/m/b"]
    assert transfers.download_argv("a/b", "/m/b", ["*Q4*.gguf", "Q4/*"])[-3:] == ["--include", "*Q4*.gguf", "Q4/*"]
    assert transfers.check_include([" *Q4*.gguf ", "", "*Q4*.gguf", "[Mm]mproj-*"]) == ["*Q4*.gguf", "[Mm]mproj-*"]
    for bad in ["-x", "/etc/*", "../*", "a b", "*;rm", "x" * 201]:
        with pytest.raises(ValueError):
            transfers.check_include([bad])
    with pytest.raises(ValueError, match="at most"):
        transfers.check_include([f"p{i}" for i in range(11)])


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
