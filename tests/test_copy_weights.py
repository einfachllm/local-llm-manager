import asyncio
import os
import time

import httpx
import pytest
import test_hosts
from conftest import FakeDocker
from fastapi.testclient import TestClient
from test_transfers import wait

from local_llm_manager.agent import create_agent_app
from local_llm_manager.executor import LocalExecutor

from local_llm_manager import config
from local_llm_manager.manager import Manager

setup = test_hosts.setup  # the hub + one agent ("mac") fixture


@pytest.fixture
def hub(setup, monkeypatch, tmp_path):
    client, agent, _, _, mac = setup
    client.app.state.manager.transfer_poll = 0.02
    monkeypatch.setattr(config, "MODEL_DIRS", [str(tmp_path / "models")])
    # Both "machines" live in this process: the agent reaches the hub's shares in-process too.
    monkeypatch.setattr(config, "PUBLIC_URL", "http://hub.local:8090")
    agent.state.executor.transfers.transport = httpx.ASGITransport(app=client.app)
    src = tmp_path / "src" / "Qwen3-8B"
    (src / "sub").mkdir(parents=True)
    (src / "model.safetensors").write_bytes(os.urandom(3_000_000))
    (src / "sub" / "config.json").write_text('{"a": 1}')
    (src / ".cache").mkdir()
    (src / ".cache" / "skip.lock").write_text("x")  # hidden folders (hf metadata) are not copied
    local = client.get("/api/hosts").json()[0]["id"]
    return client, local, mac["id"], src, tmp_path / "models" / "Qwen3-8B"


def weights(client, **locations):
    r = client.post("/api/weights", json={"name": "Qwen3-8B", "locations": locations})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_copy_from_the_hub_to_an_agent_checks_and_skips(hub):
    client, local, mac, src, dest = hub
    wid = weights(client, **{str(local): str(src)})
    res = client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]})
    assert res.status_code == 202
    items = wait(client, res.json()["id"])
    assert items[mac]["state"] == "ok", items
    assert "SHA-256 checked" in items[mac]["message"] and "from" in items[mac]["progress"]
    assert (dest / "model.safetensors").read_bytes() == (src / "model.safetensors").read_bytes()
    assert (dest / "sub" / "config.json").read_text() == '{"a": 1}' and not (dest / ".cache").exists()
    assert client.get(f"/api/weights/{wid}/hosts/{mac}").json()["state"] == "present"

    # Again: hosts that have them are "already there". With the path unregistered but the files still
    # there, nothing is transferred: same size and checksum.
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac, local]}).json()["id"])
    assert items[mac]["message"] == items[local]["message"] == "already there"
    client.put(f"/api/weights/{wid}/hosts/{mac}", json={"path": ""})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert "Copied 0 files, 2 already here" in items[mac]["message"]

    # An interrupted file continues where it stopped.
    data = (src / "model.safetensors").read_bytes()
    client.put(f"/api/weights/{wid}/hosts/{mac}", json={"path": ""})
    (dest / "model.safetensors").unlink()
    (dest / "model.safetensors.part").write_bytes(data[:1_000_000])
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert items[mac]["state"] == "ok" and (dest / "model.safetensors").read_bytes() == data


def test_copy_from_an_agent_to_the_hub(hub):
    client, local, mac, src, dest = hub
    wid = weights(client, **{str(mac): str(src)})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [local]}).json()["id"])
    assert items[local]["state"] == "ok", items
    assert (dest / "model.safetensors").read_bytes() == (src / "model.safetensors").read_bytes()


def test_a_file_that_changes_on_the_way_fails_the_checksum(hub):
    client, local, mac, src, dest = hub
    wid = weights(client, **{str(local): str(src)})
    shares = client.app.state.manager.local.shares

    async def create():
        return shares.create(str(src))  # hashes the files (cached by size and mtime)

    share = client.portal.call(create)
    deadline = time.time() + 5
    while not shares.manifest(shares.shares[share["id"]])["ready"] and time.time() < deadline:
        time.sleep(0.02)
    f = src / "model.safetensors"
    st = f.stat()
    f.write_bytes(os.urandom(st.st_size))  # same size, same mtime: the cached checksum no longer fits
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert items[mac]["state"] == "failed" and "checksum does not match" in items[mac]["message"]
    assert not (dest / "model.safetensors").exists()


def test_shares_need_their_ticket(hub):
    client, local, mac, src, dest = hub
    async def create():
        return client.app.state.manager.local.shares.create(str(src))

    share = client.portal.call(create)
    base = f"/agent/v1/shares/{share['id']}"
    assert client.get(f"{base}/manifest").status_code == 403
    assert client.get(f"{base}/manifest", headers={"x-share-ticket": "wrong"}).status_code == 403
    ok = {"x-share-ticket": share["ticket"]}
    assert client.get(f"{base}/manifest", headers=ok).status_code == 200
    assert client.get(f"{base}/files/../../etc/passwd", headers=ok).status_code == 404
    r = client.get(f"{base}/files/model.safetensors", headers={**ok, "Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == (src / "model.safetensors").read_bytes()[10:20]
    # Creating a share on an agent needs its agent token.
    agent = TestClient(create_agent_app(LocalExecutor(FakeDocker(), None)))
    assert agent.post("/agent/v1/shares", json={"path": str(src)}).status_code == 401
    assert agent.post("/agent/v1/shares", json={"path": str(src)},
                      headers={"Authorization": f"Bearer {test_hosts.TOKEN}"}).status_code != 401


def test_no_source_and_no_hub_address(hub, monkeypatch):
    client, local, mac, src, dest = hub
    wid = weights(client, **{str(mac): str(src.parent / "nothing-here")})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [local]}).json()["id"])
    assert "No host has these weights" in items[local]["message"]
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    wid2 = client.post("/api/weights", json={"name": "Local", "locations": {str(local): str(src)}}).json()["id"]
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid2, "host_ids": [mac]}).json()["id"])
    assert "address is unknown" in items[mac]["message"]


def test_distribution_is_a_snowball(tmp_path):
    """5 machines, 1 source: every finished machine serves the next one."""
    async def main():
        from local_llm_manager.db import Database
        db = Database(":memory:")
        m = Manager(db, None, None, None)
        hosts = [db.local_host_id()] + [db.create_host(f"s{i}", f"http://s{i}:8090", "t" * 40) for i in range(4)]
        wid = db.create_weight("w", "", {hosts[0]: "/w"})
        pairs = []

        async def fake_copy(weight_id, src, item, hub_url):
            pairs.append((src, item["host_id"]))
            await asyncio.sleep(0.05)
            item["state"] = "ok"

        m._copy_one = fake_copy
        items = m._host_items(hosts[1:])
        await m._distribute({"id": "x"}, wid, items, [hosts[0]], "")
        return hosts, pairs, items

    hosts, pairs, items = asyncio.run(main())
    assert all(i["state"] == "ok" for i in items)
    # Round 1: 0→1. Round 2: 0→2 and 1→3 at once. Round 3: the next free source → 4.
    assert pairs[0] == (hosts[0], hosts[1])
    assert {pairs[1], pairs[2]} == {(hosts[0], hosts[2]), (hosts[1], hosts[3])}
    assert pairs[3][1] == hosts[4]


def test_raw_get_reads_a_real_socket():
    from local_llm_manager.transfers import raw_get

    body = os.urandom(10_000_000)

    async def main():
        async def handle(reader, writer):
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            assert "x-share-ticket: t" in head and head.startswith("GET /agent/v1/shares/s/files/a HTTP/1.1")
            if "missing" in head:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        got = [x async for x in raw_get(f"http://127.0.0.1:{port}/agent/v1/shares/s/files/a", {"x-share-ticket": "t"})]
        miss = [x async for x in raw_get(f"http://127.0.0.1:{port}/agent/v1/shares/s/files/a",
                                         {"x-share-ticket": "t", "x-case": "missing"})]
        server.close()
        return got, miss

    got, miss = asyncio.run(main())
    assert got[0] == (200, b"") and b"".join(c for _, c in got) == body
    assert miss == [(404, b"")]


def test_copy_follows_hugging_face_cache_links(hub, tmp_path):
    client, local, mac, _, _ = hub
    repo = tmp_path / "hf" / "models--org--Tiny"
    (repo / "blobs").mkdir(parents=True)
    snap = repo / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (repo / "blobs" / "f00d").write_bytes(b"weights" * 1000)
    (snap / "model.safetensors").symlink_to("../../blobs/f00d")
    (snap / "evil").symlink_to("/etc/hostname")  # leaves the model: never shared
    wid = client.post("/api/weights", json={"name": "Tiny", "locations": {str(local): str(snap)}}).json()["id"]
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert items[mac]["state"] == "ok", items
    dest = tmp_path / "models" / "abc123"
    assert (dest / "model.safetensors").read_bytes() == b"weights" * 1000
    assert not (dest / "model.safetensors").is_symlink() and not (dest / "evil").exists()


def test_copy_refuses_without_disk_space(hub, monkeypatch):
    client, local, mac, src, dest = hub
    from collections import namedtuple

    from local_llm_manager import transfers
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(transfers.shutil, "disk_usage", lambda _: usage(10**12, 10**12, 10**6))
    wid = weights(client, **{str(local): str(src)})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert "Not enough disk space" in items[mac]["message"]


def test_transport_kinds():
    from local_llm_manager.executor import transport_for

    assert transport_for("") == "local"
    assert transport_for("https://spark1.example.com") == "https"
    assert transport_for("http://spark1.tail1234.ts.net:8090") == "tailscale"
    assert transport_for("http://100.101.102.103:8090") == "tailscale"
    assert transport_for("http://[fd7a:115c:a1e0::1]:8090") == "tailscale"
    assert transport_for("http://192.168.1.20:8090") == "plain"
    assert transport_for("http://127.0.0.1:8090") == "local"
    assert transport_for("http://no-such-host.invalid:8090") == "plain"
