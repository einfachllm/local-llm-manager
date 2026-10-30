"""Read-only shares of one model folder (or file), so another machine can copy it directly from here.

The hub asks this machine (with its agent token) to share a path and gets a share id plus a random ticket,
valid for a few hours. It hands both to the machine that copies; that one may then only list and read the
files below that path - it never sees this machine's agent token. Each file of a share has a SHA-256,
computed once in the background (and cached by path, size and mtime), which the copying side checks.
"""

import asyncio
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

TICKET_HEADER = "x-share-ticket"
SHARE_TTL = 6 * 3600
HASH_CHUNK = 8 * 1024 * 1024
HASH_WORKERS = 4


class BigFileResponse(FileResponse):
    # Starlette reads 64 KB at a time; multi-GB weight files go much faster in larger pieces.
    chunk_size = 4 * 1024 * 1024


def sha256_file(path: Path, progress=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK):
            h.update(chunk)
            if progress:
                progress(len(chunk))
    return h.hexdigest()


class HashCache:
    """sha256 per (real path, size, mtime): a model folder is hashed once, not on every copy."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self.data: dict[str, str] = {}
        if path and path.exists():
            try:
                self.data = json.loads(path.read_text())
            except (OSError, ValueError):
                self.data = {}

    @staticmethod
    def key(p: Path) -> str:
        st = p.stat()
        return f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"

    def get(self, p: Path) -> Optional[str]:
        try:
            return self.data.get(self.key(p))
        except OSError:
            return None

    def put(self, p: Path, digest: str) -> None:
        try:
            self.data[self.key(p)] = digest
        except OSError:
            return
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data))
                tmp.replace(self.path)
            except OSError:
                pass


def list_files(root: Path) -> list[Path]:
    """Regular files below root (or root itself), without symlinks leaving it and without partial copies."""
    if root.is_file():
        return [root]
    # Symlinks may point within the share - or, in a Hugging Face cache snapshot
    # (models--org--name/snapshots/<rev>/...), to the blobs of the same model.
    allowed = next((p for p in [root, *root.parents] if p.name.startswith("models--")), root).resolve()
    out = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))  # e.g. hf's .cache metadata
        for f in sorted(files):
            p = Path(dirpath) / f
            if f.endswith(".part") or not p.is_file():
                continue
            if p.is_symlink() and not p.resolve().is_relative_to(allowed):
                continue
            out.append(p)
    return out


class Shares:
    def __init__(self, cache: HashCache):
        self.cache = cache
        self.shares: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task] = set()

    def create(self, path: str, ttl: int = SHARE_TTL) -> dict[str, Any]:
        root = Path(os.path.expanduser(path)).resolve()
        if not root.exists():
            raise FileNotFoundError(f"{path} does not exist on this machine")
        now = time.time()
        for k in [k for k, s in self.shares.items() if s["expires"] < now]:
            self.shares.pop(k)
        files = list_files(root)
        share = {"id": secrets.token_hex(8), "ticket": secrets.token_urlsafe(32), "root": root,
                 "expires": now + ttl, "kind": "file" if root.is_file() else "dir", "name": root.name,
                 "files": [{"path": "." if root.is_file() else str(p.relative_to(root)), "abs": p,
                            "size": p.stat().st_size, "sha256": self.cache.get(p)} for p in files],
                 "hashed": 0}
        share["total"] = sum(f["size"] for f in share["files"])
        share["hashed"] = sum(f["size"] for f in share["files"] if f["sha256"])
        self.shares[share["id"]] = share
        task = asyncio.create_task(self._hash(share))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"id": share["id"], "ticket": share["ticket"], "expires": share["expires"],
                "files": len(files), "total": share["total"]}

    async def _hash(self, share: dict[str, Any]) -> None:
        sem = asyncio.Semaphore(HASH_WORKERS)

        def progress(n: int) -> None:
            share["hashed"] += n

        async def one(f: dict[str, Any]) -> None:
            if f["sha256"]:
                return
            async with sem:
                f["sha256"] = await asyncio.to_thread(sha256_file, f["abs"], progress)
                self.cache.put(f["abs"], f["sha256"])

        await asyncio.gather(*(one(f) for f in share["files"]))

    def get(self, share_id: str, ticket: str) -> dict[str, Any]:
        share = self.shares.get(share_id)
        if not share or not secrets.compare_digest(ticket.encode(), share["ticket"].encode()) \
                or share["expires"] < time.time():
            raise HTTPException(403, "Unknown or expired share")
        return share

    @staticmethod
    def manifest(share: dict[str, Any]) -> dict[str, Any]:
        ready = all(f["sha256"] for f in share["files"])
        return {"kind": share["kind"], "name": share["name"], "total": share["total"], "ready": ready,
                "hashed": min(share["hashed"], share["total"]),
                "files": [{k: f[k] for k in ("path", "size", "sha256")} for f in share["files"]] if ready else []}

    @staticmethod
    def file(share: dict[str, Any], rel: str) -> Path:
        f = next((f for f in share["files"] if f["path"] == rel), None)
        if f is None:
            raise HTTPException(404, "Not part of this share")
        return f["abs"]


def add_share_routes(app: FastAPI, shares: Shares) -> None:
    """GET routes for the copying machine; they need the share's ticket, not the agent token or a login."""

    def share_of(request: Request, share_id: str) -> dict[str, Any]:
        return shares.get(share_id, request.headers.get(TICKET_HEADER, ""))

    @app.get("/agent/v1/shares/{share_id}/manifest", include_in_schema=False)
    async def share_manifest(share_id: str, request: Request):
        return Shares.manifest(share_of(request, share_id))

    @app.get("/agent/v1/shares/{share_id}/files/{rel:path}", include_in_schema=False)
    async def share_file(share_id: str, rel: str, request: Request):
        # FileResponse answers Range requests: an interrupted copy continues where it stopped.
        return BigFileResponse(Shares.file(share_of(request, share_id), rel), media_type="application/octet-stream")
