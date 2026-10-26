import base64

import pytest
from conftest import recipe_body
from fastapi.testclient import TestClient

from local_llm_manager import config
from local_llm_manager.main import create_app

EVIL = {"Origin": "http://evil.example"}


def test_cross_site_posts_are_blocked(client):
    r = client.post("/api/recipes", json=recipe_body()).json()
    for path in (f"/api/recipes/{r['id']}/start", "/api/system/stop-all", "/api/system/free-memory",
                 f"/api/recipes/{r['id']}/duplicate"):
        res = client.post(path, headers={**EVIL, "Content-Type": "application/x-www-form-urlencoded"})
        assert res.status_code == 403, path
    assert client.delete(f"/api/recipes/{r['id']}", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/system/stop-all", headers={"Origin": "file://", "X-Forwarded-Host": ""}).status_code == 403
    # Without Origin, a fetch metadata header still marks a cross-site request.
    assert client.post("/api/system/stop-all", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_same_origin_and_non_browser_clients_still_work(client):
    assert client.post("/api/system/stop-all", headers={"Origin": "http://127.0.0.1"}).status_code == 200
    assert client.post("/api/system/stop-all").status_code == 200  # curl / scripts send no Origin
    assert client.post("/api/system/stop-all", headers={
        "Origin": "https://llm.example.com", "X-Forwarded-Host": "llm.example.com"}).status_code == 200
    assert client.get("/api/recipes", headers=EVIL).status_code == 200  # reads are not blocked by Origin


def test_security_headers(client):
    h = client.get("/api/meta").headers
    assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"


def test_without_password_only_ip_or_localhost_hosts(client):
    """DNS rebinding: an attacker's domain pointing at this machine must not reach an unprotected dashboard."""
    assert client.get("/api/meta", headers={"Host": "attacker.example:8090"}).status_code == 403
    for host in ("localhost:8090", "192.168.1.20:8090", "[::1]:8090", "127.0.0.1"):
        assert client.get("/api/meta", headers={"Host": host}).status_code == 200, host


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret")
    monkeypatch.setattr(config, "AUTH_USER", "admin")


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_basic_auth_with_password(password, fake_docker, tmp_path):
    from local_llm_manager.db import Database
    app = create_app(db=Database(str(tmp_path / "t.db")), docker=fake_docker, run_scheduler=False)
    with TestClient(app, base_url="http://spark.lan:8090") as c:
        assert c.get("/api/meta").status_code == 401
        assert c.get("/api/meta", headers=basic("admin", "wrong")).status_code == 401
        assert c.get("/api/meta", headers=basic("ädmin", "pässword")).status_code == 401  # no crash on non-ASCII
        # With a password, host names are fine (the login protects against rebinding).
        assert c.get("/api/meta", headers=basic("admin", "s3cret")).status_code == 200


def test_example_password_is_refused(monkeypatch):
    from local_llm_manager import main
    monkeypatch.setattr(config, "MODE", "hub")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "change-me")
    with pytest.raises(SystemExit, match="change-me"):
        main.run()
