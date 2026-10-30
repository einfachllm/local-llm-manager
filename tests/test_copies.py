import test_hosts
from conftest import recipe_body

setup = test_hosts.setup  # the hub + one agent ("mac") fixture

VOLS = "{weights}:/model:ro\n/data/hf:/root/.cache/huggingface"


def test_copy_to_hosts_check_create_and_sync(setup, tmp_path):
    client, _, _, mac_docker, mac = setup
    local = client.get("/api/hosts").json()[0]["id"]
    there = tmp_path / "qwen"
    there.mkdir()
    w = client.post("/api/weights", json={"name": "Qwen", "locations": {str(mac["id"]): str(there)}}).json()
    original = client.post("/api/recipes", json=recipe_body(model="/model", volumes=VOLS, weights_id=w["id"],
                                                            health_path="", tags=["team"])).json()
    url = f"/api/recipes/{original['id']}"

    (c,) = client.post(f"{url}/copies/check", json={"host_ids": [mac["id"]]}).json()["hosts"]
    assert c["weights"]["state"] == "present" and c["image"] == "missing" and c["existing_copy"] is None
    mac_docker.images = [{"repository": "nvcr.io/nvidia/vllm", "tag": "25.11-py3", "id": "x", "created": "", "size": ""}]
    (c,) = client.post(f"{url}/copies/check", json={"host_ids": [mac["id"]]}).json()["hosts"]
    assert c["image"] == "present"
    assert client.post(f"{url}/copies", json={"host_ids": [local]}).status_code == 422

    (copy,) = client.post(f"{url}/copies", json={"host_ids": [mac["id"]]}).json()["created"]
    assert copy["host_id"] == mac["id"] and copy["name"] == "Test vLLM · mac" and copy["copy_of"] == original["id"]
    assert copy["host_port"] == original["host_port"] and copy["weights_id"] == w["id"] and copy["tags"] == ["team"]
    listed = {r["id"]: r for r in client.get("/api/recipes").json()}
    assert listed[original["id"]]["copies"] == [copy["id"]] and listed[copy["id"]]["differs"] == []

    # The copy starts on its own host with that host's weights path.
    assert client.post(f"/api/recipes/{copy['id']}/start").status_code == 200
    assert f"{there}:/model:ro" in mac_docker.calls[-1]

    # Edit the original: the copy differs until synced; running copies are reported for a restart.
    client.put(url, json=recipe_body(model="/model", volumes=VOLS, weights_id=w["id"], health_path="",
                                     image="nvcr.io/nvidia/vllm:26.01-py3"))
    differs = {r["id"]: r for r in client.get("/api/recipes").json()}[copy["id"]]["differs"]
    assert "image" in differs
    res = client.post(f"{url}/sync").json()
    assert res == {"updated": [copy["id"]], "running": [copy["id"]]}
    synced = client.get(f"/api/recipes/{copy['id']}").json()
    assert synced["image"].endswith("26.01-py3") and synced["name"] == "Test vLLM · mac"

    # Copying again updates the existing copy instead of adding a second one; copying a copy uses its original.
    again = client.post(f"/api/recipes/{copy['id']}/copies", json={"host_ids": [mac["id"]]}).json()
    assert again["created"] == [] and again["updated"][0]["id"] == copy["id"]
    assert client.post(f"/api/recipes/{copy['id']}/copies", json={"host_ids": [local]}).status_code == 422

    # Deleting the original (or unlinking) makes the copy an original of its own.
    assert client.post(f"/api/recipes/{copy['id']}/unlink").json()["copy_of"] is None
    client.post(f"{url}/copies", json={"host_ids": [mac["id"]]})
    client.post(f"/api/recipes/{copy['id']}/stop")
    newest = max(r["id"] for r in client.get("/api/recipes").json())
    assert client.delete(url).status_code == 204
    assert client.get(f"/api/recipes/{newest}").json()["copy_of"] is None
