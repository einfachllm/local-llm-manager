import asyncio

from conftest import recipe_body

from local_llm_manager import config, executor as executor_mod
from local_llm_manager.executor import check_model_target, resolve_model_target

VOLS = "/home/einfachllm/models/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB:/model:ro\n/home/einfachllm/models:/models:ro"


# ---- resolve_model_target (pure string logic, hub + host side) -----------

def test_resolve_volume_exact_mount():
    t = resolve_model_target("/model", VOLS)
    assert t["kind"] == "volume"
    assert t["reference"] == "/home/einfachllm/models/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB"
    assert t["host_path"] == t["reference"]


def test_resolve_volume_parent_mount():
    # /models is mounted from ~/models, so /models/x.gguf maps under that host dir.
    t = resolve_model_target("/models/qwen.gguf", VOLS)
    assert t["kind"] == "volume"
    assert t["reference"] == "/home/einfachllm/models/qwen.gguf"


def test_resolve_hf_repo():
    t = resolve_model_target("Qwen/Qwen3-8B", "")
    assert t == {"kind": "hf", "reference": "Qwen/Qwen3-8B", "repo": "Qwen/Qwen3-8B"}


def test_resolve_hf_url_normalised():
    t = resolve_model_target("https://huggingface.co/cpatonn/Qwen3-Coder-30B", "")
    assert t["kind"] == "hf" and t["repo"] == "cpatonn/Qwen3-Coder-30B"


def test_resolve_gguf_with_slash_is_local_not_hf():
    # A path with a slash but a model file extension must not be treated as an HF repo.
    t = resolve_model_target("/models/qwen3.5-4b-Q4_K_M.gguf", VOLS)
    assert t["kind"] == "volume"  # matched the /models parent mount


def test_resolve_plain_local_path():
    t = resolve_model_target("~/models/model.gguf", "")
    assert t == {"kind": "local", "reference": "~/models/model.gguf", "path": "~/models/model.gguf"}


def test_resolve_container_path_without_mount_is_local():
    t = resolve_model_target("/model", "")
    assert t["kind"] == "local" and t["path"] == "/model"


# ---- check_model_target (host-side existence) ----------------------------

def test_check_hf_present(monkeypatch, tmp_path):
    cache = tmp_path / "hub" / "models--Qwen--Qwen3-8B"
    cache.mkdir(parents=True)
    monkeypatch.setattr(executor_mod, "_hf_cache_dir", lambda: tmp_path / "hub")
    res = check_model_target(resolve_model_target("Qwen/Qwen3-8B", ""), [])
    assert res["present"] is True and res["location"] == str(cache)


def test_check_hf_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(executor_mod, "_hf_cache_dir", lambda: tmp_path / "hub")
    res = check_model_target(resolve_model_target("Qwen/Qwen3-8B", ""), [])
    assert res["present"] is False and "models--Qwen--Qwen3-8B" in res["expected"]


def test_check_volume_present(tmp_path):
    host = tmp_path / "Nemotron"
    host.mkdir()
    vols = f"{host}:/model:ro"
    t = resolve_model_target("/model", vols)
    res = check_model_target(t, [])
    assert res["present"] is True and res["location"] == str(host)


def test_check_volume_missing(tmp_path):
    vols = f"{tmp_path}/nope:/model:ro"
    res = check_model_target(resolve_model_target("/model", vols), [])
    assert res["present"] is False and res["location"] is None


def test_check_local_relative_uses_model_dirs(monkeypatch, tmp_path):
    (tmp_path / "model.gguf").write_text("x")
    monkeypatch.setattr(executor_mod, "_hf_cache_dir", lambda: tmp_path / "empty")
    t = resolve_model_target("model.gguf", "")
    res = check_model_target(t, [str(tmp_path)])
    assert res["present"] is True and res["location"] == str(tmp_path / "model.gguf")


# ---- LocalExecutor.check_models ------------------------------------------

def test_local_executor_check_models_shapes_results(monkeypatch, tmp_path):
    monkeypatch.setattr(executor_mod, "_hf_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(config, "MODEL_DIRS", [])
    ex = executor_mod.LocalExecutor.__new__(executor_mod.LocalExecutor)
    out = asyncio.run(ex.check_models([resolve_model_target("Qwen/Qwen3-8B", "")]))
    assert out == [
        {"reference": "Qwen/Qwen3-8B", "present": False, "location": None,
         "expected": str(tmp_path / "empty" / "models--Qwen--Qwen3-8B")}
    ]


# ---- /api/models endpoint -------------------------------------------------

def test_models_overview_volume_mounted_host_path(client, tmp_path, monkeypatch):
    # A model recipe whose container path /model is backed by a real host directory.
    host_dir = tmp_path / "Nemotron"
    host_dir.mkdir()
    client.post("/api/recipes", json=recipe_body(
        name="Nemotron", model="/model",
        volumes=f"{host_dir}:/model:ro\n", host_port=8100,
    ))
    data = client.get("/api/models").json()
    [row] = data["models"]
    assert row["kind"] == "volume"
    assert row["model"] == str(host_dir)
    assert row["hosts"][0]["state"] == "present"
    assert row["hosts"][0]["location"] == str(host_dir)


def test_models_overview_hf_missing_on_local(client, monkeypatch, tmp_path):
    # Point the HF cache at an empty dir so the repo is "missing".
    monkeypatch.setattr(executor_mod, "_hf_cache_dir", lambda: tmp_path / "empty")
    client.post("/api/recipes", json=recipe_body(name="Qwen", model="Qwen/Qwen3-8B", host_port=8101))
    data = client.get("/api/models").json()
    [row] = data["models"]
    assert row["kind"] == "hf" and row["model"] == "Qwen/Qwen3-8B"
    assert row["hosts"][0]["state"] == "missing"
    assert "models--Qwen--Qwen3-8B" in row["hosts"][0]["location"]


def test_models_overview_excludes_agents_and_blank(client):
    client.post("/api/recipes", json={**recipe_body(name="Agent", host_port=8102), "kind": "agent", "model": ""})
    client.post("/api/recipes", json=recipe_body(name="Model", model="org/m", host_port=8103))
    data = client.get("/api/models").json()
    assert [m["model"] for m in data["models"]] == ["org/m"]
