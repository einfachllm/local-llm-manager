"""Long-running transfers on one machine: `docker pull` of an image and a Hugging Face download of weights.

They run as tasks in the background of the manager or agent on that machine, so a hub that loses its
connection or restarts does not interrupt them; the hub starts a task and then asks for its state.
"""

import asyncio
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config

IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,127}$")
KEEP = 50  # finished tasks kept for the hub to read


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

    def __init__(self, docker):
        self.docker = docker
        self.tasks: dict[str, dict[str, Any]] = {}
        self._running: set[asyncio.Task] = set()

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
