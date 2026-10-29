"""Long-running transfers on one machine: `docker pull` of an image and a Hugging Face download of weights.

They run as tasks in the background of the manager or agent on that machine, so a hub that loses its
connection or restarts does not interrupt them; the hub starts a task and then asks for its state.
"""

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import time
from contextlib import aclosing
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from . import config
from .shares import TICKET_HEADER, HashCache, sha256_file

IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,127}$")
KEEP = 50  # finished tasks kept for the hub to read
COPY_STREAMS = 4           # files copied at the same time (fills a 10 GbE link from one source)
COPY_CHUNK = 4 * 1024 * 1024


def check_image(image: str) -> str:
    image = image.strip()
    if not IMAGE_RE.match(image):
        raise ValueError(f"not a Docker image reference: {image!r}")
    return image


def check_repo(repo: str) -> str:
    repo = repo.strip()
    if not REPO_RE.match(repo):
        raise ValueError(f"not a Hugging Face model id (org/name): {repo!r}")
    return repo


def check_name(name: str) -> str:
    name = name.strip()
    if not NAME_RE.match(name) or ".." in name:
        raise ValueError(f"not a usable folder name: {name!r}")
    return name


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _size_gb(path: Path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1e9, 1)


def download_argv(repo: str, target: str) -> list[str]:
    """The Hugging Face CLI command for this machine: `hf` (current), else the older `huggingface-cli`."""
    for binary in (config.HF_BIN, "hf", "huggingface-cli"):
        if binary and shutil.which(binary):
            return [binary, "download", repo, "--local-dir", target]
    raise FileNotFoundError("Neither `hf` nor `huggingface-cli` is installed on this machine: "
                            "pip install -U huggingface_hub")


class Tasks:
    """Pull / download tasks of this machine. `start_*` returns at once; `get(id)` tells the progress."""

    def __init__(self, docker, cache: Optional[HashCache] = None):
        self.docker = docker
        self.cache = cache or HashCache(None)
        self.tasks: dict[str, dict[str, Any]] = {}
        self._running: set[asyncio.Task] = set()
        # How copies reach the source machine (tests route it to an in-process app).
        self.transport: Optional[httpx.AsyncBaseTransport] = None

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        return self.tasks.get(task_id)

    def _new(self, kind: str, target: str, **extra) -> tuple[dict[str, Any], bool]:
        # The same pull / download running already (e.g. asked twice): follow that one.
        for t in self.tasks.values():
            if t["kind"] == kind and t["target"] == target and t["state"] == "running":
                return t, False
        task = {"id": secrets.token_hex(6), "kind": kind, "target": target, "state": "running", "message": "",
                "progress": "", "started": _now(), "finished": None, **extra}
        self.tasks[task["id"]] = task
        done = [k for k, t in self.tasks.items() if t["state"] != "running"]
        for k in done[:max(0, len(done) - KEEP)]:
            self.tasks.pop(k)
        return task, True

    def _spawn(self, task: dict[str, Any], coro) -> None:
        async def run():
            try:
                task["message"] = await coro
                task["state"] = "ok"
            except Exception as e:  # noqa: BLE001 - reported to the hub, never raised
                task["state"], task["message"] = "failed", str(e) or type(e).__name__
            task["finished"] = _now()

        t = asyncio.create_task(run())
        self._running.add(t)
        t.add_done_callback(self._running.discard)

    def start_pull(self, image: str) -> dict[str, Any]:
        image = check_image(image)
        task, new = self._new("pull", image)
        if new:
            self._spawn(task, self._pull(task, image))
        return task

    async def _pull(self, task: dict[str, Any], image: str) -> str:
        def on_line(line: str) -> None:
            if line.strip():
                task["progress"] = line.strip()[:200]

        res = await self.docker.pull(image, on_line)
        if not res.ok:
            raise RuntimeError(res.error.splitlines()[-1] if res.error else f"docker pull exited with {res.returncode}")
        return f"Pulled {image}"

    def start_copy(self, source: str, share_id: str, ticket: str, name: str) -> dict[str, Any]:
        """Copy a share of another machine (see shares.py) into this machine's model dir as `name`."""
        if not re.match(r"^https?://[^\s/]+$", source.rstrip("/")):
            raise ValueError(f"not a source address: {source!r}")
        name = check_name(name)
        target = Path(os.path.expanduser(config.MODEL_DIRS[0] if config.MODEL_DIRS else "~/models")) / name
        # Keyed by the target folder: a second copy into the same folder follows the running one.
        task, new = self._new("copy", str(target), path=str(target))
        if new:
            task["progress"] = "connecting to the source"
            self._spawn(task, Copy(self, task, source, share_id, ticket, target).run())
        return task

    def start_download(self, repo: str, name: str = "") -> dict[str, Any]:
        repo = check_repo(repo)
        name = check_name(name or repo.rsplit("/", 1)[-1])
        target = Path(os.path.expanduser(config.MODEL_DIRS[0] if config.MODEL_DIRS else "~/models")) / name
        task, new = self._new("download", repo, path=str(target))
        if new:
            self._spawn(task, self._download(task, repo, target))
        return task

    async def _download(self, task: dict[str, Any], repo: str, target: Path) -> str:
        argv = download_argv(repo, str(target))
        target.mkdir(parents=True, exist_ok=True)
        # HF_TOKEN (gated models) comes from this machine's own environment / .env.
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        tail: list[str] = []

        async def read() -> None:
            assert proc.stdout
            while chunk := await proc.stdout.read(4096):
                # tqdm redraws with \r: keep the last few real lines for an error message.
                for part in re.split(r"[\r\n]+", chunk.decode(errors="replace")):
                    if part.strip():
                        tail[:] = (tail + [part.strip()])[-5:]

        async def measure() -> None:
            while True:
                task["progress"] = f"{await asyncio.to_thread(_size_gb, target)} GB"
                await asyncio.sleep(2)

        meter = asyncio.create_task(measure())
        try:
            await read()
            code = await proc.wait()
        finally:
            meter.cancel()
        size = await asyncio.to_thread(_size_gb, target)
        task["progress"] = f"{size} GB"
        if code != 0:
            raise RuntimeError(f"{argv[0]} download exited with {code}: {tail[-1] if tail else ''}".strip())
        return f"Downloaded {repo} to {target} ({size} GB)"


def fmt_progress(done: int, total: int, rate: float) -> str:
    gb = lambda n: f"{n / 1e9:.1f}"  # noqa: E731
    out = f"{gb(done)} / {gb(total)} GB"
    if rate > 1e6:
        left = (total - done) / rate
        out += f" · {rate / 1e6:.0f} MB/s · {int(left // 60)}:{int(left % 60):02d} left"
    return out


def _safe_target(root: Path, rel: str) -> Path:
    p = (root / rel).resolve() if rel != "." else root
    if p != root and not p.is_relative_to(root.resolve()):
        raise ValueError(f"refusing a path outside the target: {rel}")
    return p


class Copy:
    """Copy one share from another machine into this machine's model dir: several files at once,
    each checked against the source's SHA-256 while it is written, files that are already here (same
    size and checksum) skipped, and an interrupted file continued from where it stopped (.part)."""

    def __init__(self, tasks: "Tasks", task: dict[str, Any], source: str, share_id: str, ticket: str,
                 target: Path):
        self.tasks, self.task, self.target = tasks, task, target
        self.base = f"{source.rstrip('/')}/agent/v1/shares/{share_id}"
        self.headers = {TICKET_HEADER: ticket}
        self.done = 0
        self.total = 0
        self.started = time.monotonic()
        self.copied_bytes = 0

    def _show(self) -> None:
        elapsed = max(time.monotonic() - self.started, 1e-3)
        self.task["progress"] = fmt_progress(self.done, self.total, self.copied_bytes / elapsed)

    async def run(self) -> str:
        timeout = httpx.Timeout(60.0, connect=5.0)
        async with httpx.AsyncClient(headers=self.headers, timeout=timeout, transport=self.tasks.transport) as c:
            manifest = await self._manifest(c)
            self.total = manifest["total"]
            files = manifest["files"]
            root = self.target if manifest["kind"] == "dir" else self.target.parent
            root.mkdir(parents=True, exist_ok=True)
            # Enough room? (files already here with the right size are not counted)
            def present(f: dict[str, Any]) -> bool:
                d = self.target if f["path"] == "." else self.target / f["path"]
                return d.exists() and d.stat().st_size == f["size"]
            needed = sum(f["size"] for f in files if not present(f))
            free = shutil.disk_usage(root).free
            if needed > free - 2e9:
                raise RuntimeError(f"Not enough disk space in {root}: {needed / 1e9:.1f} GB needed, "
                                   f"{free / 1e9:.1f} GB free")
            self.started = time.monotonic()
            self._show()
            sem = asyncio.Semaphore(COPY_STREAMS)
            counts = {"copied": 0, "skipped": 0}

            async def one(f: dict[str, Any]) -> None:
                async with sem:
                    dest = self.target if f["path"] == "." else _safe_target(self.target, f["path"])
                    counts["copied" if await self._file(c, f, dest) else "skipped"] += 1

            await asyncio.gather(*(one(f) for f in files))
        self.done = self.total
        self._show()
        n, skipped = counts["copied"], counts["skipped"]
        here = f", {skipped} already here" if skipped else ""
        return (f"Copied {n} file{'s' if n != 1 else ''}{here} to {self.target} "
                f"({self.total / 1e9:.1f} GB, SHA-256 checked)")

    async def _manifest(self, c: httpx.AsyncClient) -> dict[str, Any]:
        # The source hashes its files first (once; cached there): wait for it, showing how far it is.
        while True:
            r = await c.get(f"{self.base}/manifest")
            if r.status_code != 200:
                raise RuntimeError(f"source answered {r.status_code}: {r.text[:200]}")
            m = r.json()
            if m["ready"]:
                return m
            pct = int(100 * m["hashed"] / m["total"]) if m["total"] else 0
            self.task["progress"] = f"source is computing checksums: {pct} %"
            await asyncio.sleep(1.0)

    async def _file(self, c: httpx.AsyncClient, f: dict[str, Any], dest: Path) -> bool:
        """Copy one file; False when it was already here."""
        if dest.exists() and dest.stat().st_size == f["size"]:
            have = self.tasks.cache.get(dest) or await asyncio.to_thread(sha256_file, dest)
            if have == f["sha256"]:
                self.tasks.cache.put(dest, have)
                self.done += f["size"]
                self._show()
                return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        for attempt in (1, 2):
            digest = await self._download(c, f, part)
            if digest == f["sha256"]:
                part.replace(dest)
                self.tasks.cache.put(dest, digest)
                return True
            part.unlink(missing_ok=True)  # damaged on the way: once more from the start
            self.done -= f["size"]
            if attempt == 2:
                raise RuntimeError(f"{f['path']}: checksum does not match the source's (twice)")
        return True

    async def _download(self, c: httpx.AsyncClient, f: dict[str, Any], part: Path) -> str:
        h = hashlib.sha256()
        offset = part.stat().st_size if part.exists() else 0
        if offset > f["size"]:
            part.unlink()
            offset = 0
        if offset:  # continue an interrupted file: its first part counts towards the checksum
            def rehash() -> None:
                with open(part, "rb") as fh:
                    while chunk := fh.read(COPY_CHUNK):
                        h.update(chunk)
            await asyncio.to_thread(rehash)
        self.done += offset
        if offset < f["size"]:
            headers = {**self.headers, **({"Range": f"bytes={offset}-"} if offset else {})}
            url = f"{self.base}/files/{f['path']}"
            # A plain socket read is about 3x faster than httpx for multi-GB bodies; httpx stays for
            # tests, whose source is an in-process app.
            body = raw_get(url, headers) if self.tasks.transport is None else _httpx_get(c, url, headers)
            fh = None
            try:
                async with aclosing(body):  # closes the connection also when the copy fails halfway
                    async for status, chunk in body:
                        if fh is None:
                            if status not in (200, 206):
                                raise RuntimeError(f"{f['path']}: source answered {status}")
                            if offset and status == 200:  # no range support: start over
                                h, offset = hashlib.sha256(), 0
                            fh = open(part, "ab" if offset else "wb")
                        if chunk:
                            await asyncio.to_thread(_write_hash, fh, h, chunk)
                            self.done += len(chunk)
                            self.copied_bytes += len(chunk)
                            self._show()
            finally:
                if fh:
                    fh.close()
        return h.hexdigest()


async def _httpx_get(c: httpx.AsyncClient, url: str, headers: dict[str, str]):
    """(status, chunk) pairs of a GET; the first pair carries the status and may have an empty chunk."""
    async with c.stream("GET", url, headers=headers) as r:
        yield r.status_code, b""
        if r.status_code in (200, 206):
            async for chunk in r.aiter_bytes(COPY_CHUNK):
                yield r.status_code, chunk


async def raw_get(url: str, headers: dict[str, str], timeout: float = 60.0):
    """A minimal HTTP/1.1 GET for big file bodies (Content-Length, no chunked encoding - what the shares
    send), yielding (status, chunk) like _httpx_get. https works too; proxies are not used (LAN copies)."""
    import ssl
    from urllib.parse import urlsplit

    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    ctx = ssl.create_default_context() if u.scheme == "https" else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(u.hostname, port, ssl=ctx, limit=2 * COPY_CHUNK), timeout)
    try:
        path = u.path + (f"?{u.query}" if u.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {u.netloc}", "Connection: close",
                 *(f"{k}: {v}" for k, v in headers.items())]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)).decode("latin-1")
        status = int(head.split(" ", 2)[1])
        fields = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in head.split("\r\n")[1:]) if k}
        if "chunked" in fields.get("transfer-encoding", ""):
            raise RuntimeError("unexpected chunked response from the source")
        yield status, b""
        if status not in (200, 206):
            return
        left = int(fields.get("content-length", "-1"))
        while left != 0:
            chunk = await asyncio.wait_for(reader.read(COPY_CHUNK if left < 0 else min(COPY_CHUNK, left)), timeout)
            if not chunk:
                if left > 0:
                    raise RuntimeError("connection to the source closed early")
                break
            left -= len(chunk) if left > 0 else 0
            yield status, chunk
    finally:
        writer.close()


def _write_hash(fh, h, chunk: bytes) -> None:
    fh.write(chunk)
    h.update(chunk)
