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
from local_llm_manager.processes import ProcessRunner

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


def test_a_share_reports_a_symlinked_shard_once(tmp_path):
    """A shard that is only a link to a file of the same share travels as a link: the manifest counts
    the bytes once, and the link needs no checksum of its own."""
    from local_llm_manager.shares import HashCache, Shares

    src = tmp_path / "Qwen3.8"
    (src / "tensors").mkdir(parents=True)
    (src / "tensors" / "model.safetensors").write_bytes(b"z" * 1000)
    (src / "model.safetensors").symlink_to("tensors/model.safetensors")
    (src / "config.json").write_text("{}")

    async def manifest():
        shares = Shares(HashCache(None))
        info = shares.create(str(src))
        for _ in range(200):
            m = Shares.manifest(shares.shares[info["id"]])
            if m["ready"]:
                return m
            await asyncio.sleep(0.01)
        raise AssertionError("the share never became ready")

    m = asyncio.run(manifest())
    assert m["version"] == 2
    assert m["total"] == 1002, "the linked shard was counted next to its target"
    by_path = {f["path"]: f for f in m["files"]}
    assert set(by_path) == {"model.safetensors", "tensors/model.safetensors", "config.json"}
    assert by_path["model.safetensors"]["link"] == "tensors/model.safetensors"
    assert by_path["model.safetensors"]["size"] == 0 and by_path["model.safetensors"]["sha256"] is None
    assert by_path["tensors/model.safetensors"]["sha256"]


def test_share_stores_link_targets_relative(tmp_path):
    """An absolute symlink of the source folder travels as a relative one: the source machine's
    paths do not exist on the machine that copies."""
    from local_llm_manager.shares import HashCache, Shares
    src = tmp_path / "m"
    (src / "tensors").mkdir(parents=True)
    (src / "tensors" / "a.safetensors").write_bytes(b"x" * 10)
    (src / "a.safetensors").symlink_to(src / "tensors" / "a.safetensors")  # absolute on purpose

    async def entries():
        shares = Shares(HashCache(None))
        info = shares.create(str(src))
        return shares.shares[info["id"]]["files"]

    by_path = {f["path"]: f for f in asyncio.run(entries())}
    assert by_path["a.safetensors"]["link"] == "tensors/a.safetensors"


@pytest.mark.parametrize("kind", ["absolute", "escaping"])
def test_copy_refuses_links_leaving_the_target(hub, monkeypatch, kind):
    """A manipulated source must not plant absolute or escaping links on the copying machine."""
    client, local, mac, src, dest = hub
    (src / "alias").symlink_to("model.safetensors")
    wid = weights(client, **{str(local): str(src)})
    shares = client.app.state.manager.local.shares
    evil = str(src / "model.safetensors") if kind == "absolute" else "../../evil"
    original = shares.create

    def tampered(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        for f in shares.shares[info["id"]]["files"]:
            if f.get("link") is not None:
                f["link"] = evil
        return info

    monkeypatch.setattr(shares, "create", tampered)
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    fragment = "absolute" if kind == "absolute" else "outside"
    assert items[mac]["state"] == "failed", items
    assert "refusing" in items[mac]["message"] and fragment in items[mac]["message"]
    assert not (dest / "alias").is_symlink() and not (dest / "alias").exists()
    assert not (dest.parent.parent / "evil").exists()


def test_a_symlinked_shard_arrives_as_a_link(hub):
    """The same folder on the target machine, not 100 GB of it twice over."""
    client, local, mac, src, dest = hub
    data = (src / "model.safetensors").read_bytes()
    (src / "tensors").mkdir()
    (src / "tensors" / "model.safetensors").write_bytes(data)
    (src / "model.safetensors").unlink()
    (src / "model.safetensors").symlink_to("tensors/model.safetensors")

    wid = weights(client, **{str(local): str(src)})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert items[mac]["state"] == "ok", items[mac]
    assert os.path.islink(dest / "model.safetensors"), "the link arrived as a second copy of the shard"
    assert (dest / "tensors" / "model.safetensors").read_bytes() == data
    assert (dest / "model.safetensors").read_bytes() == data  # through the link
    assert os.path.islink(dest / "sub") is False

    # Again: the link is skipped like a file that is already here - and the real shard stays a real
    # file (the link's destination must not be resolved into the path that gets replaced).
    client.put(f"/api/weights/{wid}/hosts/{mac}", json={"path": ""})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert "already here" in items[mac]["message"]
    assert os.path.islink(dest / "model.safetensors")
    assert not (dest / "tensors" / "model.safetensors").is_symlink()
    assert (dest / "tensors" / "model.safetensors").read_bytes() == data


def test_a_share_lists_a_linked_directory_once(tmp_path):
    """A symlinked subdirectory travels as a link entry; its files are listed once, under the real path."""
    from local_llm_manager.shares import HashCache, Shares
    src = tmp_path / "Qwen3.8"
    (src / "shards").mkdir(parents=True)
    (src / "shards" / "model.safetensors").write_bytes(b"z" * 1000)
    (src / "tensors").symlink_to("shards", target_is_directory=True)
    (src / "config.json").write_text("{}")

    async def manifest():
        shares = Shares(HashCache(None))
        info = shares.create(str(src))
        for _ in range(200):
            m = Shares.manifest(shares.shares[info["id"]])
            if m["ready"]:
                return m
            await asyncio.sleep(0.01)
        raise AssertionError("the share never became ready")

    m = asyncio.run(manifest())
    assert m["total"] == 1002, "the linked directory's files were counted next to their real path"
    by_path = {f["path"]: f for f in m["files"]}
    assert set(by_path) == {"shards/model.safetensors", "tensors", "config.json"}
    assert by_path["tensors"]["link"] == "shards"
    assert by_path["tensors"]["size"] == 0 and by_path["tensors"]["sha256"] is None
    assert by_path["shards/model.safetensors"]["sha256"]


def test_a_symlinked_subdirectory_arrives_as_a_link(hub):
    """The target gets the source's layout: the linked directory is a link again, and the files
    below it are reachable through it. A second copy skips the link like it skips files."""
    client, local, mac, src, dest = hub
    data = (src / "model.safetensors").read_bytes()
    (src / "shards").mkdir()
    (src / "shards" / "model.safetensors").write_bytes(data)
    (src / "model.safetensors").unlink()
    (src / "tensors").symlink_to("shards", target_is_directory=True)

    wid = weights(client, **{str(local): str(src)})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert items[mac]["state"] == "ok", items
    assert os.path.islink(dest / "tensors") and os.readlink(dest / "tensors") == "shards"
    assert (dest / "shards" / "model.safetensors").read_bytes() == data
    assert (dest / "tensors" / "model.safetensors").read_bytes() == data  # through the link

    client.put(f"/api/weights/{wid}/hosts/{mac}", json={"path": ""})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert "already here" in items[mac]["message"]


def test_copy_refuses_to_replace_a_real_directory_with_a_link(hub):
    """A real directory at the target keeps its content: the copy fails clearly instead of deleting it."""
    client, local, mac, src, dest = hub
    (src / "shards").mkdir()
    (src / "shards" / "model.safetensors").write_bytes(b"x" * 10)
    (src / "tensors").symlink_to("shards", target_is_directory=True)
    (dest / "tensors").mkdir(parents=True)
    (dest / "tensors" / "precious.txt").write_text("keep me")

    wid = weights(client, **{str(local): str(src)})
    items = wait(client, client.post("/api/jobs/copy", json={"weight_id": wid, "host_ids": [mac]}).json()["id"])
    assert items[mac]["state"] == "failed", items
    assert "real directory" in items[mac]["message"]
    assert not (dest / "tensors").is_symlink()
    assert (dest / "tensors" / "precious.txt").read_text() == "keep me"


# ---- compatibility: targets from before links existed (0.2.0) ----------------------------------


def test_links_as_files_expands_linked_directories(tmp_path):
    """links_as_files emits only real file entries (size + sha256, no link key) - the format 0.2.0
    targets read. A linked directory's files travel below its path; a cycle does not hang."""
    from local_llm_manager.shares import HashCache, Shares
    src = tmp_path / "m"
    (src / "shards").mkdir(parents=True)
    (src / "shards" / "a.safetensors").write_bytes(b"z" * 1000)
    (src / "tensors").symlink_to("shards", target_is_directory=True)
    (src / "loop").symlink_to(".", target_is_directory=True)  # points back into its own folder
    (src / "config.json").write_text("{}")

    async def manifest():
        shares = Shares(HashCache(None))
        info = shares.create(str(src), links_as_files=True)
        for _ in range(200):
            m = Shares.manifest(shares.shares[info["id"]])
            if m["ready"]:
                return m
            await asyncio.sleep(0.01)
        raise AssertionError("the share never became ready")

    m = asyncio.run(manifest())
    assert m["version"] == 2
    assert all("link" not in f for f in m["files"]), "an old target cannot recreate links"
    by_path = {f["path"]: f for f in m["files"]}
    assert by_path["tensors/a.safetensors"]["size"] == 1000
    assert by_path["tensors/a.safetensors"]["sha256"] == by_path["shards/a.safetensors"]["sha256"]
    assert "loop/shards/a.safetensors" in by_path  # the cycle was expanded once, not endlessly


def test_compat_share_copies_as_real_files(setup, monkeypatch, tmp_path):
    """A share with links_as_files=True is consumable without any link support: every entry arrives
    as a real file with the source's bytes (what a 0.2.0 target does with path/size/sha256)."""
    client, agent, _, _, mac = setup
    client.app.state.manager.transfer_poll = 0.02
    monkeypatch.setattr(config, "MODEL_DIRS", [str(tmp_path / "models")])
    monkeypatch.setattr(config, "PUBLIC_URL", "http://hub.local:8090")
    agent.state.executor.transfers.transport = httpx.ASGITransport(app=client.app)
    src = tmp_path / "src" / "Qwen3-8B"
    (src / "tensors").mkdir(parents=True)
    data = os.urandom(100_000)
    (src / "tensors" / "model.safetensors").write_bytes(data)
    (src / "model.safetensors").symlink_to("tensors/model.safetensors")
    (src / "config.json").write_text("{}")
    shares = client.app.state.manager.local.shares

    async def create():
        return shares.create(str(src), links_as_files=True)

    info = client.portal.call(create)
    task = client.portal.call(lambda: agent.state.executor.transfers.start_copy(
        "http://hub.local:8090", info["id"], info["ticket"], "Compat"))
    deadline = time.time() + 10
    while task["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert task["state"] == "ok", task

    m = shares.manifest(shares.shares[info["id"]])
    assert all("link" not in f for f in m["files"])
    by_path = {f["path"]: f for f in m["files"]}
    assert by_path["model.safetensors"]["size"] == len(data)
    assert by_path["model.safetensors"]["sha256"] == by_path["tensors/model.safetensors"]["sha256"]
    dest = tmp_path / "models" / "Compat"
    assert not (dest / "model.safetensors").is_symlink()
    assert (dest / "model.safetensors").read_bytes() == data
    assert (dest / "tensors" / "model.safetensors").read_bytes() == data


def test_agent_share_endpoint_links_as_files(setup, tmp_path):
    """The agent endpoint takes the optional flag; without it (old hubs) links travel as links."""
    src = tmp_path / "m"
    (src / "tensors").mkdir(parents=True)
    (src / "tensors" / "a.safetensors").write_bytes(b"x" * 10)
    (src / "a.safetensors").symlink_to("tensors/a.safetensors")
    agent = TestClient(create_agent_app(LocalExecutor(FakeDocker(), ProcessRunner(tmp_path / "procs"))))
    auth = {"Authorization": f"Bearer {test_hosts.TOKEN}"}
    for body, expect_link in (({"path": str(src)}, True),
                              ({"path": str(src), "links_as_files": False}, True),
                              ({"path": str(src), "links_as_files": True}, False)):
        r = agent.post("/agent/v1/shares", json=body, headers=auth)
        assert r.status_code == 200, r.text
        entries = agent.app.state.executor.shares.shares[r.json()["id"]]["files"]
        assert any("link" in f for f in entries) == expect_link, body
    assert agent.post("/agent/v1/shares", json={"path": str(tmp_path / "gone")}, headers=auth).status_code == 404
