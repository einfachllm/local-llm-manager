import pytest

from local_llm_manager.recipes import GgufChoiceError, gguf_files, resolve_gguf

from conftest import recipe_body


def touch(folder, *names):
    for n in names:
        p = folder / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"GGUF")


def test_gguf_files_skips_projectors_and_later_split_parts(tmp_path):
    touch(tmp_path, "Qwen3-VL-Q4_K_M.gguf", "mmproj-Qwen3-VL-F16.gguf", "README.md",
          "big/Big-Q8_0-00001-of-00003.gguf", "big/Big-Q8_0-00002-of-00003.gguf", "big/Big-Q8_0-00003-of-00003.gguf",
          ".cache/hidden.gguf", "a/b/c/too-deep.gguf")
    assert gguf_files(str(tmp_path)) == ["Qwen3-VL-Q4_K_M.gguf", "big/Big-Q8_0-00001-of-00003.gguf"]


def native(model):
    return {"framework": "llamacpp-native", "runtime": "process", "model": model}


def docker(host_path, model="/model"):
    return {"framework": "llamacpp", "runtime": "docker", "model": model,
            "volumes": f"{host_path}:/model:ro\n~/models:/models"}


def test_native_folder_with_one_model(tmp_path):
    touch(tmp_path, "Qwen3-8B-Q4_K_M.gguf", "mmproj-x.gguf")
    assert resolve_gguf(native(str(tmp_path)))["model"] == f"{tmp_path}/Qwen3-8B-Q4_K_M.gguf"


def test_docker_mount_of_a_folder(tmp_path):
    touch(tmp_path, "m/Big-00001-of-00002.gguf", "m/Big-00002-of-00002.gguf")
    assert resolve_gguf(docker(tmp_path))["model"] == "/model/m/Big-00001-of-00002.gguf"


def test_left_alone(tmp_path):
    touch(tmp_path, "a.gguf")
    file_recipe = native(str(tmp_path / "a.gguf"))
    assert resolve_gguf(file_recipe) is file_recipe                        # already a file
    explicit = docker(tmp_path, model="/models/a.gguf")
    assert resolve_gguf(explicit) is explicit                              # model is not a mount of its own
    vllm = {**native(str(tmp_path)), "framework": "vllm"}
    assert resolve_gguf(vllm) is vllm                                      # other engines take folders
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_gguf(native(str(empty)))["model"] == str(empty)         # nothing to pick: llama.cpp reports it


def test_several_models_need_a_choice(tmp_path):
    touch(tmp_path, "x-Q4_K_M.gguf", "x-Q8_0.gguf")
    with pytest.raises(GgufChoiceError, match="x-Q4_K_M.gguf, x-Q8_0.gguf"):
        resolve_gguf(native(str(tmp_path)))


def llamacpp_recipe(client, folder, **over):
    w = client.post("/api/weights", json={"name": "Qwen3-8B-GGUF", "locations": {"1": str(folder)}}).json()
    body = recipe_body(framework="llamacpp", image="ghcr.io/ggml-org/llama.cpp:server-cuda", model="/model",
                       command="-m {model} --host {bind_host} --port {port}", args="-ngl 999",
                       volumes="{weights}:/model:ro", env="", docker_args="", weights_id=w["id"], container_port=8080)
    body.update(over)
    return client.post("/api/recipes", json=body).json()


def test_start_picks_the_gguf_in_a_downloaded_folder(client, fake_docker, tmp_path):
    touch(tmp_path, "Qwen3-8B-Q4_K_M.gguf", "README.md")
    r = llamacpp_recipe(client, tmp_path)
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 200, res.text
    argv = next(c for c in fake_docker.calls if "--name" in c)
    assert argv[argv.index("-m") + 1] == "/model/Qwen3-8B-Q4_K_M.gguf"
    assert f"{tmp_path}:/model:ro" in argv


def test_start_refuses_an_ambiguous_folder(client, fake_docker, tmp_path):
    touch(tmp_path, "x-Q4_K_M.gguf", "x-Q8_0.gguf")
    r = llamacpp_recipe(client, tmp_path)
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 409
    assert "several GGUF models" in res.json()["detail"] and "/model/x-Q4_K_M.gguf" in res.json()["detail"]
    assert not any("--name" in c for c in fake_docker.calls)
