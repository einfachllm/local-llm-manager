import os
from pathlib import Path

import test_hosts
from conftest import recipe_body

from local_llm_manager import config

setup = test_hosts.setup  # the hub + one agent ("mac") fixture

VOLS = "{weights}:/model:ro\n/data/hf:/root/.cache/huggingface"


def weight(client, name="Nemotron", **locations):
    r = client.post("/api/weights", json={"name": name, "source": "nvidia/Nemotron", "locations": locations})
    assert r.status_code == 201, r.text
    return r.json()


def test_weights_crud(client, tmp_path):
    local = client.get("/api/hosts").json()[0]["id"]
    w = weight(client, **{str(local): str(tmp_path)})
    assert w["locations"] == {str(local): str(tmp_path)}
    assert client.post("/api/weights", json={"name": "Nemotron"}).status_code == 409
    assert client.post("/api/weights", json={"name": "x", "locations": {str(local): "/a:/b"}}).status_code == 422
    assert client.post("/api/weights", json={"name": "y", "locations": {"999": "/a"}}).status_code == 404

    w = client.put(f"/api/weights/{w['id']}", json={"name": "Nemotron 2", "locations": {}}).json()
    assert w["name"] == "Nemotron 2" and w["locations"] == {}
    assert client.put(f"/api/weights/{w['id']}/hosts/{local}", json={"path": str(tmp_path)}).json()["state"] == "present"
    assert client.get(f"/api/weights/{w['id']}/hosts/{local}").json()["path"] == str(tmp_path)
    assert client.put(f"/api/weights/{w['id']}/hosts/{local}", json={"path": ""}).json()["state"] == "unset"

    r = client.post("/api/recipes", json=recipe_body(model="/model", volumes=VOLS, weights_id=w["id"])).json()
    assert r["weights_id"] == w["id"]
    assert client.delete(f"/api/weights/{w['id']}").status_code == 409
    assert client.post("/api/recipes", json=recipe_body(name="b", container_name="b", weights_id=999)).status_code == 422
    client.delete(f"/api/recipes/{r['id']}")
    assert client.delete(f"/api/weights/{w['id']}").status_code == 204


def test_preview_fills_the_hosts_path(client, tmp_path):
    local = client.get("/api/hosts").json()[0]["id"]
    w = weight(client, **{str(local): "~/models/Nemotron"})
    body = recipe_body(model="/model", volumes=VOLS, weights_id=w["id"])
    cmd = client.post("/api/recipes/preview", json=body).json()["command"]
    assert "/models/Nemotron:/model:ro" in cmd and "{weights}" not in cmd
    cmd = client.post("/api/recipes/preview", json={**body, "weights_id": None}).json()["command"]
    assert "{weights}:/model:ro" in cmd


def test_moving_a_recipe_mounts_the_other_hosts_path(setup, tmp_path):
    client, _, hub_docker, mac_docker, mac = setup
    local = client.get("/api/hosts").json()[0]["id"]
    here, there = tmp_path / "spark1-models", tmp_path / "nvme" / "nemotron"
    here.mkdir()
    there.mkdir(parents=True)
    w = weight(client, **{str(local): str(here), str(mac["id"]): str(there)})
    r = client.post("/api/recipes", json=recipe_body(model="/model", volumes=VOLS, weights_id=w["id"],
                                                     health_path="")).json()

    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    assert f"{here}:/model:ro" in hub_docker.calls[-1]
    client.post(f"/api/recipes/{r['id']}/stop")

    moved = client.put(f"/api/recipes/{r['id']}", json=recipe_body(model="/model", volumes=VOLS, weights_id=w["id"],
                                                                   health_path="", host_id=mac["id"]))
    assert moved.status_code == 200, moved.text
    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    argv = mac_docker.calls[-1]
    assert f"{there}:/model:ro" in argv and not any("{weights}" in a for a in argv)

    overview = {c["host_id"]: c for c in client.get("/api/weights").json()["weights"][0]["hosts"]}
    assert overview[local]["state"] == "present" and overview[mac["id"]]["state"] == "present"


def test_start_refuses_without_weights_on_the_host(client, fake_docker, tmp_path):
    local = client.get("/api/hosts").json()[0]["id"]
    w = weight(client)
    r = client.post("/api/recipes", json=recipe_body(model="/model", volumes=VOLS, weights_id=w["id"])).json()
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 409 and "no path on" in res.json()["detail"]

    client.put(f"/api/weights/{w['id']}/hosts/{local}", json={"path": str(tmp_path / "gone")})
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 409 and "not found on" in res.json()["detail"]
    assert client.get("/api/weights").json()["weights"][0]["hosts"][0]["state"] == "missing"

    plain = client.post("/api/recipes", json=recipe_body(name="p", container_name="p", host_port=8101,
                                                         model="/model", volumes=VOLS)).json()
    res = client.post(f"/api/recipes/{plain['id']}/start")
    assert res.status_code == 409 and "no weights selected" in res.json()["detail"]
    assert not fake_docker.calls


def test_start_refuses_the_model_folder_registered_instead_of_its_parent(client, fake_docker, tmp_path):
    """The qwen38-sglang.yaml trap: the recipe mounts {weights} at /models and reads
    /models/Qwen3.8-27B-NVFP4 from it, but the weights entry is the model folder itself. The mount
    sits one level too deep, and the engine crashes with a Hugging Face repo-id error - refuse first."""
    local = client.get("/api/hosts").json()[0]["id"]
    model_folder = tmp_path / "Qwen3.8-27B-NVFP4"
    model_folder.mkdir()
    w = weight(client, name="Qwen3.8-27B-NVFP4", **{str(local): str(model_folder)})
    body = recipe_body(model="/models/Qwen3.8-27B-NVFP4", volumes="{weights}:/models:ro", weights_id=w["id"])
    r = client.post("/api/recipes", json=body).json()

    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert "model folder itself" in detail and "parent folder" in detail
    assert not fake_docker.calls

    # The fix the message names: register the parent folder (model and drafter inside) and pick it.
    (tmp_path / "Qwen3.8-27B-DFlash2").mkdir()
    parent = weight(client, name="Qwen3.8-Models", **{str(local): str(tmp_path)})
    assert client.put(f"/api/recipes/{r['id']}", json={**body, "weights_id": parent["id"]}).status_code == 200
    assert client.post(f"/api/recipes/{r['id']}/start").status_code == 200
    assert f"{tmp_path}:/models:ro" in fake_docker.calls[-1]


def test_start_refuses_a_missing_model_subfolder_of_the_weights(client, fake_docker, tmp_path):
    """A weights folder that simply lacks the subfolder the recipe reads: same refusal, generic wording."""
    local = client.get("/api/hosts").json()[0]["id"]
    w = weight(client, name="Models", **{str(local): str(tmp_path)})
    body = recipe_body(model="/models/Sub", volumes="{weights}:/models:ro", weights_id=w["id"])
    r = client.post("/api/recipes", json=body).json()
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 409 and "does not exist" in res.json()["detail"]
    assert "model folder itself" not in res.json()["detail"]
    assert not fake_docker.calls


def test_start_refuses_a_native_model_subfolder_missing_from_the_weights(client, tmp_path):
    """The tensorfold example's version of the trap: model {weights}/Qwen3.8-27B-NVFP4 with the model
    folder itself registered resolves to <folder>/Qwen3.8-27B-NVFP4, which is not there."""
    local = client.get("/api/hosts").json()[0]["id"]
    model_folder = tmp_path / "Qwen3.8-27B-NVFP4"
    model_folder.mkdir()
    w = weight(client, name="Qwen3.8-27B-NVFP4", **{str(local): str(model_folder)})
    body = recipe_body(runtime="process", framework="tensorfold", image="",
                       command="tensorfold serve {model} --host {bind_host} --port {port}",
                       model="{weights}/Qwen3.8-27B-NVFP4", volumes="", docker_args="", gpus="",
                       weights_id=w["id"])
    r = client.post("/api/recipes", json=body).json()
    res = client.post(f"/api/recipes/{r['id']}/start")
    assert res.status_code == 409, res.text
    assert "model folder itself" in res.json()["detail"]


def test_deleting_a_host_drops_its_paths(setup, tmp_path):
    client, _, _, _, mac = setup
    w = weight(client, **{str(mac["id"]): "/srv/models/x"})
    assert client.delete(f"/api/hosts/{mac['id']}").status_code == 204
    assert client.get("/api/weights?check=false").json()["weights"][0]["locations"] == {}
    assert w["id"]


def test_recipes_on_registered_weights_leave_the_detected_list(client, tmp_path):
    local = client.get("/api/hosts").json()[0]["id"]
    w = weight(client, **{str(local): str(tmp_path)})
    client.post("/api/recipes", json=recipe_body(model="/model", volumes=VOLS, weights_id=w["id"]))
    client.post("/api/recipes", json=recipe_body(name="q", container_name="q", host_port=8101, model="Qwen/Qwen3-8B"))
    assert [m["model"] for m in client.get("/api/models").json()["models"]] == ["Qwen/Qwen3-8B"]


# ---- step 2: search the hosts, adopt existing recipes ----------------------

def test_scan_finds_model_folders_and_ggufs(tmp_path, monkeypatch):
    from local_llm_manager.executor import scan_weight_dirs
    (tmp_path / "Nemotron-VL").mkdir()
    (tmp_path / "Nemotron-VL" / "config.json").write_text("{}")
    (tmp_path / "Nemotron-VL" / "model-00001.safetensors").write_bytes(b"x" * 10)
    (tmp_path / "Nemotron-VL" / "sub").mkdir()
    (tmp_path / "Nemotron-VL" / "sub" / "config.json").write_text("{}")  # inside a model: not listed
    (tmp_path / "gguf").mkdir()
    (tmp_path / "gguf" / "qwen3-4b-Q4_K_M.gguf").write_bytes(b"x")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "config.json").write_text("{}")
    (tmp_path / "notes").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    found = {f["name"]: f for f in scan_weight_dirs(["~"])}
    assert set(found) == {"Nemotron-VL", "qwen3-4b-Q4_K_M.gguf"}
    assert found["Nemotron-VL"]["path"] == "~/Nemotron-VL" and found["Nemotron-VL"]["kind"] == "dir"
    assert found["qwen3-4b-Q4_K_M.gguf"]["path"] == "~/gguf/qwen3-4b-Q4_K_M.gguf"


def test_discover_suggests_paths_on_other_hosts(setup, tmp_path, monkeypatch):
    from local_llm_manager import config
    client, _, _, _, mac = setup
    local = client.get("/api/hosts").json()[0]["id"]
    models = tmp_path / "models"
    for name in ("NVIDIA-Nemotron-VL", "Qwen3-8B"):
        (models / name).mkdir(parents=True)
        (models / name / "config.json").write_text("{}")
    monkeypatch.setattr(config, "MODEL_DIRS", [str(models)])  # hub and agent run in this process
    w = weight(client, name="Nemotron VL", **{str(local): str(models / "NVIDIA-Nemotron-VL")})

    d = client.get("/api/weights/discover").json()
    assert [h["count"] for h in d["hosts"]] == [2, 2]
    [s] = d["suggestions"]
    assert s["weight_id"] == w["id"] and s["host_id"] == mac["id"] and s["path"].endswith("NVIDIA-Nemotron-VL")
    assert sorted((f["host_id"], f["name"]) for f in d["unregistered"]) == [(local, "Qwen3-8B"), (mac["id"], "Qwen3-8B")]


def test_adopt_moves_recipes_onto_one_entry(setup, tmp_path):
    client, _, _, mac_docker, mac = setup
    vols = "~/models/Nemotron-VL:/model:ro\n/data/hf:/root/.cache/huggingface"
    a = client.post("/api/recipes", json=recipe_body(model="/model", volumes=vols)).json()
    b = client.post("/api/recipes", json=recipe_body(name="b", container_name="b", host_port=8101, host_id=mac["id"],
                                                     model="/model", volumes="/nvme/Nemotron-VL:/model")).json()
    hf = client.post("/api/recipes", json=recipe_body(name="c", container_name="c", host_port=8102)).json()

    rows = {m["model"]: m for m in client.get("/api/models").json()["models"]}
    assert rows["~/models/Nemotron-VL"]["adoptable"] and not rows["Qwen/Qwen3-8B"]["adoptable"]

    assert client.post("/api/weights/adopt", json={"recipe_ids": [hf["id"]]}).status_code == 422
    w = client.post("/api/weights/adopt", json={"recipe_ids": [a["id"]]}).json()
    assert w["name"] == "Nemotron-VL"
    w = client.post("/api/weights/adopt", json={"recipe_ids": [b["id"]]}).json()  # same folder name: same entry
    assert w["locations"] == {str(a["host_id"]): "~/models/Nemotron-VL", str(mac["id"]): "/nvme/Nemotron-VL"}

    a2 = client.get(f"/api/recipes/{a['id']}").json()
    assert a2["weights_id"] == w["id"] and a2["volumes"].splitlines()[0] == "{weights}:/model:ro"
    assert client.get(f"/api/recipes/{b['id']}").json()["volumes"] == "{weights}:/model"
    assert [m["model"] for m in client.get("/api/models").json()["models"]] == ["Qwen/Qwen3-8B"]


def test_one_model_is_one_row_whatever_the_case(client, tmp_path):
    """`UNIQUE(name)` is case-sensitive, so `qwen3.8` used to land next to `Qwen3.8` - and the tab
    sorts NOCASE, which puts the two rows of one model right beside each other."""
    local = str(client.get("/api/hosts").json()[0]["id"])
    w = weight(client, "Qwen3.8-27B", **{local: str(tmp_path / "Qwen3.8-27B")})

    for name in ("qwen3.8-27b", "  QWEN3.8-27B  ", "Qwen3.8-27B"):
        r = client.post("/api/weights", json={"name": name, "locations": {}})
        assert r.status_code == 409, (name, r.text)
        assert "already exist" in r.text

    other = weight(client, "Nemotron", **{local: str(tmp_path / "nemotron")})
    r = client.put(f"/api/weights/{other['id']}", json={"name": "qwen3.8-27B", "locations": {}})
    assert r.status_code == 409 and "already exist" in r.text
    assert client.put(f"/api/weights/{w['id']}", json={"name": "Qwen3.8-27B", "locations": {}}).status_code == 200


def test_one_folder_on_a_host_is_one_row(client, tmp_path):
    """Two rows for one folder mean two downloads and two copies of the same weights."""
    local = str(client.get("/api/hosts").json()[0]["id"])
    folder = tmp_path / "Qwen3.8-27B-NVFP4"
    w = weight(client, "Qwen3.8-27B-NVFP4", **{local: str(folder)})

    r = client.post("/api/weights", json={"name": "nvfp4-again", "locations": {local: f"{folder}/"}})
    assert r.status_code == 409 and "already registered" in r.text and "Qwen3.8-27B-NVFP4" in r.text

    # Stored without the spelling that differs, so `{weights}` mounts identically everywhere.
    assert w["locations"][local] == str(folder)
    r = client.put(f"/api/weights/{w['id']}/hosts/{local}", json={"path": f"  {folder}//  "})
    assert r.status_code == 200 and r.json()["path"] == str(folder)
    # The folder that holds several models is something else: still allowed (see the example recipes).
    assert client.post("/api/weights", json={"name": "models", "locations": {local: str(tmp_path)}}).status_code == 201


def test_one_folder_is_one_row_across_spellings(client):
    """~/models/x and /home/u/models/x are one folder: the duplicate check compares canonically
    (expanduser + normpath), not just the stored spelling."""
    local = str(client.get("/api/hosts").json()[0]["id"])
    weight(client, "Canonical", **{local: "~/models/canon-x"})
    for spell in (str(Path.home() / "models" / "canon-x"), "~/models/./canon-x",
                  "~/models/sub/../canon-x", "~/models/canon-x/"):
        r = client.post("/api/weights", json={"name": "other", "locations": {local: spell}})
        assert r.status_code == 409, (spell, r.text)
        assert "already registered" in r.text
    assert client.post("/api/weights", json={"name": "other",
                                             "locations": {local: "~/models/canon-y"}}).status_code == 201


def test_discover_matches_canonical_paths(setup, tmp_path, monkeypatch):
    """A registered absolute path and the scan's ~/... spelling are one folder: the found model is
    not offered as 'unregistered' news."""
    client, _, _, _, mac = setup
    local = client.get("/api/hosts").json()[0]["id"]
    monkeypatch.setenv("HOME", str(tmp_path))
    models = tmp_path / "models"
    (models / "Canon").mkdir(parents=True)
    (models / "Canon" / "config.json").write_text("{}")
    monkeypatch.setattr(config, "MODEL_DIRS", [str(models)])  # hub and agent run in this process
    weight(client, name="Canon", **{str(local): str(models / "Canon")})  # absolute spelling

    d = client.get("/api/weights/discover").json()
    assert all(f["name"] != "Canon" for f in d["unregistered"]), d["unregistered"]


def test_a_symlinked_shard_does_not_double_the_weights_size(tmp_path):
    """A Hugging Face download may keep its shards in `tensors/` and link them at the top. `du` counts
    such a file once; `os.stat` follows the link, so the Weights tab showed a 96 GB model as 204 GB."""
    from local_llm_manager import executor

    folder = tmp_path / "models" / "Qwen3.8-Flash-Next"
    (folder / "tensors").mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    shard = folder / "tensors" / "model-00001-of-00001.safetensors"
    shard.touch()
    os.truncate(shard, 500 * 1000 * 1000)  # sparse: 0.5 GB of st_size, nothing written
    (folder / "model-00001-of-00001.safetensors").symlink_to("tensors/model-00001-of-00001.safetensors")

    assert executor._size_gb(str(folder)) == 0.5
    assert [f["size_gb"] for f in executor.scan_weight_dirs([str(tmp_path / "models")])] == [0.5]
