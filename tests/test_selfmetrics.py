"""Self-monitoring: /healthz (always) and the opt-in /metrics endpoint (DGXM_METRICS=1)."""

import pytest
from fastapi.testclient import TestClient

from local_llm_manager import config
from local_llm_manager.db import Database
from local_llm_manager.main import create_app
from local_llm_manager.selfmetrics import Sink


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret")
    monkeypatch.setattr(config, "AUTH_USER", "admin")


def test_sink_render():
    s = Sink()
    s.add("app_requests_total", "Requests.", method="GET", path="/api/meta", status="200")
    s.add("app_requests_total", "Requests.", method="GET", path="/api/meta", status="200")
    s.gauge("app_uptime_seconds", "Uptime.", 12.5)
    s.counter("app_actions_total", "Actions.", 2, action="start", result="ok")
    text = s.render()
    assert "# TYPE app_requests_total counter" in text
    assert '# HELP app_requests_total Requests.' in text
    assert 'app_requests_total{method="GET",path="/api/meta",status="200"} 2' in text
    assert "app_uptime_seconds 12.5" in text
    assert 'app_actions_total{action="start",result="ok"} 2' in text
    assert s.uptime() >= 0


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_healthz_reports_unreachable_database(client, monkeypatch):
    monkeypatch.setattr(client.app.state.db, "ping", lambda: False)
    r = client.get("/healthz")
    assert r.status_code == 503 and "database" in r.text


def test_metrics_disabled_by_default(client):
    assert client.get("/metrics").status_code == 404


def test_metrics_enabled_shows_self_metrics(client, monkeypatch):
    monkeypatch.setattr(config, "METRICS_ENABLED", True)
    client.get("/api/meta")  # one counted, routed request
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert 'dgxm_build_info{mode="hub",version="0.1.0"} 1' in body
    assert "dgxm_uptime_seconds" in body
    assert "dgxm_recipes_total 0" in body
    assert "dgxm_hosts_total 1" in body
    assert 'dgxm_api_requests_total{method="GET",path="/api/meta",status="200"} 1' in body


def test_metrics_skips_login(password, fake_docker, tmp_path, monkeypatch):
    """Monitors scrape /healthz and /metrics without a dashboard login."""
    monkeypatch.setattr(config, "METRICS_ENABLED", True)
    app = create_app(db=Database(str(tmp_path / "t.db")), docker=fake_docker, run_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/meta").status_code == 401  # the API still needs the login
        assert c.get("/healthz").status_code == 200
        assert c.get("/metrics").status_code == 200


def test_agent_healthz_and_metrics(fake_docker, tmp_path, monkeypatch):
    from local_llm_manager.agent import create_agent_app
    from local_llm_manager.executor import LocalExecutor
    from local_llm_manager.processes import ProcessRunner

    monkeypatch.setattr(config, "AGENT_TOKEN", "tok" * 16)
    ex = LocalExecutor(procs=ProcessRunner(str(tmp_path / "procs")))
    app = create_agent_app(executor=ex)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/agent/v1/info").status_code == 401  # the API still needs the token
        assert c.get("/metrics").status_code == 404  # not enabled yet
    monkeypatch.setattr(config, "METRICS_ENABLED", True)
    with TestClient(app) as c:
        body = c.get("/metrics").text
        assert 'dgxm_build_info{mode="agent",version="0.1.0"} 1' in body
        assert "dgxm_uptime_seconds" in body
        # A token-less call to a protected route is counted as a request (routed, then rejected):
        # one in the first block, one here.
        c.get("/agent/v1/info")
        assert 'dgxm_api_requests_total{method="GET",path="/agent/v1/info",status="401"} 2' in c.get("/metrics").text
