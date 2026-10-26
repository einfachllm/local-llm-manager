"""Thin async wrapper around the docker CLI."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Optional

from . import config


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def error(self) -> str:
        return (self.stderr or self.stdout).strip()


async def run(argv: list[str], timeout: float = 60.0) -> CommandResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {argv[0]}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CommandResult(124, "", f"timed out after {timeout:.0f}s: {' '.join(argv[:3])}")
    return CommandResult(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


def split_image(ref: str) -> tuple[str, str]:
    """`vllm/vllm-openai:v0.11.0` -> ("vllm/vllm-openai", "v0.11.0"). Docker Hub prefixes are dropped, as in
    `docker image ls`; a missing tag is "latest"; a digest reference keeps "@sha256:..." as its tag."""
    ref = ref.strip()
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        repo, tag = repo, "@" + digest
    else:
        colon, slash = ref.rfind(":"), ref.rfind("/")
        repo, tag = (ref[:colon], ref[colon + 1:]) if colon > slash else (ref, "latest")
    for prefix in ("docker.io/library/", "docker.io/", "index.docker.io/library/", "index.docker.io/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
            break
    if repo.startswith("library/"):
        repo = repo[len("library/"):]
    return repo, tag


def _created_iso(created_at: str) -> str:
    """`2026-10-20 14:03:11 +0200 CEST` (docker image ls) -> ISO 8601; unparseable values are kept."""
    try:
        return datetime.strptime(" ".join(created_at.split()[:3]), "%Y-%m-%d %H:%M:%S %z").isoformat()
    except ValueError:
        return created_at


class Docker:
    """Container operations. Swappable in tests."""

    def __init__(self, binary: str = config.DOCKER_BIN):
        self.binary = binary

    async def run_argv(self, argv: list[str]) -> CommandResult:
        # Image pulls on first start can take a while.
        return await run(argv, timeout=1800)

    async def list_containers(self) -> dict[str, dict]:
        """All containers keyed by name: {state, status, id, image}."""
        res = await run([self.binary, "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=15)
        containers: dict[str, dict] = {}
        if not res.ok:
            return containers
        for line in res.stdout.splitlines():
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue
            for name in c.get("Names", "").split(","):
                containers[name] = {
                    "id": c.get("ID", "")[:12],
                    "state": c.get("State", ""),
                    "status": c.get("Status", ""),
                    "image": c.get("Image", ""),
                }
        return containers

    async def list_images(self) -> list[dict]:
        """Local images: {repository, tag, id, created (ISO), size}. Dangling `<none>` images are skipped."""
        res = await run([self.binary, "image", "ls", "--format", "{{json .}}"], timeout=30)
        if not res.ok:
            raise RuntimeError(res.error or "docker image ls failed")
        images = []
        for line in res.stdout.splitlines():
            try:
                i = json.loads(line)
            except json.JSONDecodeError:
                continue
            if i.get("Repository", "<none>") == "<none>" or i.get("Tag", "<none>") == "<none>":
                continue
            images.append({"repository": i["Repository"], "tag": i["Tag"], "id": i.get("ID", "")[-12:],
                           "created": _created_iso(i.get("CreatedAt", "")), "size": i.get("Size", "")})
        return images

    async def stop(self, name: str, timeout: int = config.STOP_TIMEOUT) -> CommandResult:
        return await run([self.binary, "stop", "-t", str(timeout), name], timeout=timeout + 30)

    async def remove(self, name: str) -> CommandResult:
        return await run([self.binary, "rm", "-f", name], timeout=60)

    async def logs(self, name: str, tail: int = 500) -> CommandResult:
        return await run([self.binary, "logs", "--timestamps", "--tail", str(tail), name], timeout=30)

    async def follow_logs(self, name: str, tail: int = 200) -> AsyncIterator[str]:
        proc = await asyncio.create_subprocess_exec(
            self.binary, "logs", "--timestamps", "-f", "--tail", str(tail), name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                yield line.decode(errors="replace").rstrip("\n")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def drop_caches(self, image: str = config.DROP_CACHES_IMAGE) -> CommandResult:
        """Flush the page cache via a privileged throwaway container (docker access == root anyway)."""
        return await run(
            [self.binary, "run", "--rm", "--privileged", image, "sh", "-c",
             "sync && echo 3 > /proc/sys/vm/drop_caches"],
            timeout=300,
        )


async def try_drop_caches_locally() -> Optional[str]:
    """Drop caches directly if we run as root on the host. Returns None on success, else an error."""
    try:
        res = await run(["sync"], timeout=120)
        if not res.ok:
            return res.error
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
        return None
    except OSError as e:
        return str(e)
