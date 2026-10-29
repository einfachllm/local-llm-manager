from conftest import recipe_body

from local_llm_manager.recipes import set_flag

BASE_VOLS = "~/models/Old-Model:/model:ro\n/data/hf:/root/.cache/huggingface"
BASE_ARGS = "--served-model-name old\n--max-model-len 524288\n--tool-call-parser qwen3_coder\n--enable-prefix-caching"


def base_recipe(client, **overrides):
    r = client.post("/api/recipes", json=recipe_body(name="Old model", model="/model", volumes=BASE_VOLS,
                                                     args=BASE_ARGS, **overrides))
    assert r.status_code == 201, r.text
    return r.json()


def test_set_flag_replaces_or_adds():
    assert set_flag("--a 1\n--max-model-len 4096 --b", ("--max-model-len",), "8192", None) == (
        "--a 1\n--max-model-len 8192 --b", True)
    assert set_flag("--max-model-len=4096", ("--max-model-len",), "8192", None)[0] == "--max-model-len=8192"
    assert set_flag("--a 1\n", ("-c",), "8192", "-c") == ("--a 1\n-c 8192", True)
    assert set_flag("--a 1", ("-c",), "8192", None) == ("--a 1", False)


def test_quick_recipe_from_an_existing_recipe_and_a_found_folder(client, tmp_path):
    local = client.get("/api/hosts").json()[0]["id"]
    base = base_recipe(client)
    folder = tmp_path / "New-Model-FP4"
    folder.mkdir()
    body = {"host_id": local, "base_recipe_id": base["id"], "path": str(folder), "context": 65536}

    draft = client.post("/api/recipes/quick", json={**body, "save": False}).json()
    assert draft["recipe"]["name"] == "New-Model-FP4"
    assert f"{folder}:/model:ro" in draft["command"] and "Old-Model" not in draft["command"]
    assert "--served-model-name new-model-fp4" in draft["command"] and "--max-model-len 65536" in draft["command"]
    assert any("--tool-call-parser" in n for n in draft["notes"])
    assert not client.get("/api/weights?check=false").json()["weights"]  # a draft registers nothing

    res = client.post("/api/recipes/quick", json=body)
    assert res.status_code == 200, res.text
    r = res.json()["recipe"]
    assert r["host_port"] == base["host_port"]  # same port as the base: switchable
    assert r["volumes"].startswith("{weights}:/model:ro") and r["image"] == base["image"]
    (w,) = client.get("/api/weights?check=false").json()["weights"]
    assert w["name"] == "New-Model-FP4" and r["weights_id"] == w["id"]

    # Again with the same folder: the registered entry is reused, names stay unique.
    r2 = client.post("/api/recipes/quick", json=body).json()["recipe"]
    assert r2["weights_id"] == w["id"] and r2["name"] == "New-Model-FP4 2" and r2["container_name"] != r["container_name"]
    assert len(client.get("/api/weights?check=false").json()["weights"]) == 1


def test_quick_recipe_from_a_preset_and_a_hf_id(client):
    res = client.post("/api/recipes/quick", json={"framework": "vllm", "model": "Qwen/Qwen3-8B", "context": 32768})
    assert res.status_code == 200, res.text
    r = res.json()["recipe"]
    assert r["name"] == "Qwen3-8B" and r["model"] == "Qwen/Qwen3-8B" and r["weights_id"] is None
    assert "--max-model-len 32768" in r["args"] and "--served-model-name qwen3-8b" in r["args"]
    assert r["host_port"] == 8100 and r["gpus"] == "all"

    native = client.post("/api/recipes/quick", json={"framework": "llamacpp-native", "path": "/m/x.gguf"}).json()
    assert native["recipe"]["model"] == "{weights}" and "--alias x" in native["recipe"]["args"]
    assert native["recipe"]["host_port"] == 8101


def test_quick_recipe_rejects_bad_input(client):
    base = base_recipe(client)
    assert client.post("/api/recipes/quick", json={"framework": "vllm"}).status_code == 422
    assert client.post("/api/recipes/quick", json={"framework": "nope", "model": "a/b"}).status_code == 422
    assert client.post("/api/recipes/quick", json={"framework": "vllm", "model": "a/b", "path": "/x"}).status_code == 422
    assert client.post("/api/recipes/quick", json={"framework": "vllm", "path": "/a:/b"}).status_code == 422
    assert client.post("/api/recipes/quick", json={"base_recipe_id": 999, "model": "a/b"}).status_code == 404
    # A HF id with a base whose model is a mount: the mount goes, the model becomes the id.
    r = client.post("/api/recipes/quick", json={"base_recipe_id": base["id"], "model": "org/m"}).json()["recipe"]
    assert r["model"] == "org/m" and "Old-Model" not in r["volumes"]
