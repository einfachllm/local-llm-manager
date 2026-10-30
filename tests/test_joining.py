import asyncio
import re

import httpx

from local_llm_manager import config
from local_llm_manager.joining import Joiner, join_code

TOKEN = "a" * 40


def body(**over):
    return {"name": "spark3", "url": "http://spark3:8090", "token": TOKEN, "version": "0.1.0",
            "info": {"platform": "linux", "machine": "aarch64"}, **over}


def test_join_code():
    assert re.fullmatch(r"[A-Z2-7]{4}-[A-Z2-7]{4}", join_code(TOKEN))
    assert join_code(TOKEN) == join_code(TOKEN) != join_code("b" * 40)


def test_join_approve_and_move(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret-password-123")
    login = {"Authorization": "Basic " + __import__("base64").b64encode(b"admin:s3cret-password-123").decode()}

    # Agents need no login to ask; the list needs one and never shows the token.
    res = client.post("/api/join", json=body())
    assert res.status_code == 200 and res.json() == {"state": "pending", "code": join_code(TOKEN)}
    assert client.get("/api/join-requests").status_code == 401
    (req,) = client.get("/api/join-requests", headers=login).json()
    assert req["code"] == join_code(TOKEN) and req["name"] == "spark3" and "token" not in req
    assert req["info"]["machine"] == "aarch64" and req["transport"] == "plain"

    # Asking again updates the same request.
    client.post("/api/join", json=body(url="http://spark3:8091"))
    (req,) = client.get("/api/join-requests", headers=login).json()
    assert req["url"] == "http://spark3:8091"

    host = client.post(f"/api/join-requests/{req['id']}/approve", json={}, headers=login).json()
    assert host["name"] == "spark3" and host["url"] == "http://spark3:8091"
    assert client.get("/api/join-requests", headers=login).json() == []
    assert client.post("/api/join", json=body(url="http://spark3:8091")).json() == {"state": "approved"}

    # The approved agent (same token) may report a new address; another token may not take its place.
    client.post("/api/join", json=body(url="http://10.0.0.7:8090"))
    hosts = {h["name"]: h for h in client.get("/api/hosts", headers=login).json()}
    assert hosts["spark3"]["url"] == "http://10.0.0.7:8090"
    assert client.post("/api/join", json=body(token="c" * 40)).json()["state"] == "pending"
    (other,) = client.get("/api/join-requests", headers=login).json()
    res = client.post(f"/api/join-requests/{other['id']}/approve", json={}, headers=login)
    assert res.status_code == 409  # "spark3" exists at another address
    assert client.post(f"/api/join-requests/{other['id']}/approve", json={"name": "spark3-b"},
                       headers=login).json()["name"] == "spark3-b"


def test_join_reject_validation_and_limit(client):
    client.post("/api/join", json=body())
    (req,) = client.get("/api/join-requests").json()
    assert client.post(f"/api/join-requests/{req['id']}/reject").status_code == 204
    assert client.post("/api/join", json=body()).json() == {"state": "rejected"}
    assert client.post(f"/api/join-requests/{req['id']}/approve", json={}).status_code == 409
    assert client.post("/api/join-requests/999/approve", json={}).status_code == 404

    assert client.post("/api/join", json=body(token="short")).status_code == 422
    assert client.post("/api/join", json=body(url="file:///etc/passwd", token="d" * 40)).status_code == 422
    assert client.post("/api/join", json=body(name="a b", token="d" * 40)).status_code == 422

    for i in range(20):
        assert client.post("/api/join", json=body(name=f"s{i}", token=f"{i:02d}" * 20)).status_code == 200
    assert client.post("/api/join", json=body(name="one-more", token="e" * 40)).status_code == 429


def test_joiner_follows_the_hub(client, monkeypatch):
    monkeypatch.setattr(config, "AGENT_URL", "http://spark9:8090")
    monkeypatch.setattr(config, "AGENT_NAME", "spark9")
    joiner = Joiner("http://hub:8090", TOKEN, {"platform": "linux"}, httpx.ASGITransport(app=client.app))

    async def announce():
        return await joiner.announce()

    assert client.portal.call(announce) == "pending"
    assert joiner.state["code"] == join_code(TOKEN) and joiner.state["url"] == "http://spark9:8090"
    (req,) = client.get("/api/join-requests").json()
    client.post(f"/api/join-requests/{req['id']}/approve", json={})
    assert client.portal.call(announce) == "approved"

    unreachable = Joiner("http://nowhere.invalid:8090", TOKEN)
    assert asyncio.run(unreachable.announce()) == "error" and "not reachable" in unreachable.state["message"]
