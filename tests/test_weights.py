from conftest import recipe_body
from test_hosts import setup  # noqa: F401  (hub + one agent "mac")

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
