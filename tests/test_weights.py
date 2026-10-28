import test_hosts
from conftest import recipe_body

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
