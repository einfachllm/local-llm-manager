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
    for host in ("attacker.example:8090", "spark.attacker.example", "ts.net.attacker.example"):
        assert client.get("/api/meta", headers={"Host": host}).status_code == 403, host
    for host in ("localhost:8090", "192.168.1.20:8090", "[::1]:8090", "127.0.0.1", "100.64.0.7:8090",
                 "spark:8090", "spark.tail1234.ts.net", "mac-studio.local:8090"):
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


@pytest.mark.parametrize("pw,ok", [
    ("change-me", False), ("Password", False), ("admin", False), ("short-pw", False), ("aaaaaaaaaaaaaa", False),
    ("correct-horse-battery", True), ("Xk3_9vQ-2mPz", True), ("my-admin-pass-2026", False),
])
def test_password_policy(pw, ok):
    from local_llm_manager.main import password_problem
    assert (password_problem(pw, "admin") is None) == ok


@pytest.mark.parametrize("mode,setting,value", [
    ("hub", "AUTH_PASSWORD", "change-me"), ("hub", "AUTH_PASSWORD", "too-short"),
    ("agent", "AGENT_TOKEN", "abc123"),
])
def test_weak_secrets_refuse_to_start(monkeypatch, capsys, mode, setting, value):
    from local_llm_manager import main
    monkeypatch.setattr(main.os, "umask", lambda mask: 0)  # keep the test process's umask
    monkeypatch.setattr(config, "MODE", mode)
    monkeypatch.setattr(config, setting, value)
    with pytest.raises(SystemExit) as exc:
        main.run()
    assert exc.value.code == main.EX_CONFIG  # the systemd unit does not restart on this
    assert setting.replace("AUTH_", "DGXM_").replace("AGENT_TOKEN", "DGXM_AGENT_TOKEN") in capsys.readouterr().err


def test_failed_logins_are_throttled(password, fake_docker, tmp_path):
    from local_llm_manager.db import Database
    app = create_app(db=Database(str(tmp_path / "t.db")), docker=fake_docker, run_scheduler=False)
    app.state.login_throttle.max_failures = 3
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/meta").status_code == 401  # no credentials yet: not counted
        for _ in range(3):
            assert c.get("/api/meta", headers=basic("admin", "guess")).status_code == 401
        locked = c.get("/api/meta", headers=basic("admin", "s3cret"))
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
        app.state.login_throttle._failures.clear()  # window over
        assert c.get("/api/meta", headers=basic("admin", "s3cret")).status_code == 200
        # A successful login resets the count.
        for _ in range(2):
            c.get("/api/meta", headers=basic("admin", "guess"))
        assert c.get("/api/meta", headers=basic("admin", "s3cret")).status_code == 200
        assert c.get("/api/meta", headers=basic("admin", "guess")).status_code == 401


def test_installer_env_gets_random_secrets(tmp_path):
    import shutil
    from pathlib import Path
    from local_llm_manager import envfile
    env = tmp_path / ".env"
    shutil.copy(Path(__file__).parent.parent / ".env.example", env)
    pw = envfile.fill(env)
    text = env.read_text()
    assert pw and len(pw) >= 20 and f"DGXM_PASSWORD={pw}" in text and "change-me" not in text.split("DGXM_PASSWORD=")[1].split("\n")[0]
    token = text.split("DGXM_AGENT_TOKEN=")[1].split("\n")[0]
    assert len(token) == 64 and oct(env.stat().st_mode & 0o777) == "0o600"
    from local_llm_manager.main import password_problem
    assert password_problem(pw) is None
    assert envfile.fill(env) is None  # running again keeps the password


def test_database_file_is_private(tmp_path):
    from local_llm_manager.db import Database
    Database(str(tmp_path / "sub" / "manager.db"))
    assert oct((tmp_path / "sub" / "manager.db").stat().st_mode & 0o777) == "0o600"


def test_publish_ip_binds_ports_and_native_servers(monkeypatch):
    from local_llm_manager import recipes
    monkeypatch.setattr(config, "PUBLISH_IP", "100.64.0.7")
    docker = recipes.build_run_argv({**recipe_body(command="vllm serve {model} --host {bind_host} --port {port}"),
                                     "container_name": "m"})
    assert "100.64.0.7:8100:8000" in docker and docker[docker.index("--host") + 1] == "0.0.0.0"
    native = recipes.build_process_argv({"runtime": "process", "command": "mlx_lm.server --host {bind_host}",
                                         "model": "m", "host_port": 8080, "container_port": 8080})
    assert native == ["mlx_lm.server", "--host", "100.64.0.7"]
    host_net = recipes.build_run_argv({**recipe_body(command="serve --host {bind_host}", docker_args="--network host"),
                                       "container_name": "m"})
    assert "-p" not in host_net and host_net[host_net.index("--host") + 1] == "100.64.0.7"


def test_tailscale_setting_resolves_or_explains(monkeypatch):
    for name in ("HOST", "PUBLISH_IP", "HEALTH_HOST"):
        monkeypatch.setattr(config, name, getattr(config, name))  # restored after the test
    monkeypatch.setattr(config, "HOST", "tailscale")
    monkeypatch.setattr(config, "PUBLISH_IP", "tailscale")
    monkeypatch.delenv("DGXM_HEALTH_HOST", raising=False)
    real_lookup = config.tailscale_ip
    monkeypatch.setattr(config, "tailscale_ip", lambda: "100.64.0.7")
    config.resolve_network()
    assert config.HOST == config.PUBLISH_IP == config.HEALTH_HOST == "100.64.0.7"

    monkeypatch.setattr(config, "TAILSCALE_CLI", ("no-such-tailscale-binary",))
    with pytest.raises(SystemExit, match="Tailscale"):
        real_lookup()
