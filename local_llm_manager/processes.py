"""Native-process runtime: run a model server directly on the host (e.g. MLX on a Mac).

Each process is started in its own session/process group through a tiny bash wrapper that
records the exit code, so a crash is visible even after the manager restarts. State lives in
<state_dir>/<name>.json, output in <name>.log (the previous run's log is kept as <name>.log.1).
Processes keep running when the manager stops, like containers do.
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

import psutil

from . import config

# Runs the server, then stores its exit code. Not `exec`, so the code can be written afterwards.
WRAPPER = '"$@"; code=$?; echo "$code" > "$0"; exit "$code"'

# A running native log is rotated (copytruncate) once it grows past this size; one previous generation
# is kept as <name>.log.1. start() opens the log in append mode, so truncation does not lose the writer.
LOG_MAX_BYTES = 50 * 1024 * 1024
_TAIL_CHUNK = 64 * 1024


class ProcessError(Exception):
    pass


def _tail_lines(path: Path, tail: int) -> list[str]:
    """The last `tail` lines of a file without reading it all: seek backwards from the end in chunks
    until enough newlines are found. Matches read().splitlines()[-tail:], including files with or without
    a trailing newline, so a multi-GB log does not have to fit in memory."""
    if tail <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        remaining = f.tell()
        if remaining == 0:
            return []
        blocks: list[bytes] = []
        newlines = 0
        # Stop once we have more than `tail` newlines: that guarantees `tail` complete lines even if the
        # first block starts mid-line. For short files we simply reach the start.
        while remaining > 0 and newlines <= tail:
            step = min(_TAIL_CHUNK, remaining)
            remaining -= step
            f.seek(remaining)
            block = f.read(step)
            blocks.append(block)
            newlines += block.count(b"\n")
    data = b"".join(reversed(blocks))
    return data.decode(errors="replace").splitlines()[-tail:]


class ProcessRunner:
    def __init__(self, state_dir: Path | str):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # Popen handles of processes started by this manager instance, so they get reaped.
        self._children: dict[str, subprocess.Popen] = {}

    def _paths(self, name: str) -> tuple[Path, Path, Path]:
        return self.dir / f"{name}.json", self.dir / f"{name}.log", self.dir / f"{name}.exit"

    def _read_state(self, name: str) -> Optional[dict]:
        state_file, _, _ = self._paths(name)
        try:
            return json.loads(state_file.read_text())
        except (OSError, ValueError):
            return None

    def _alive(self, state: dict) -> bool:
        """True if the recorded PID still is the process we started (guards against PID reuse)."""
        child = self._children.get(state.get("name", ""))
        if child is not None and child.pid == state["pid"]:
            child.poll()  # reap if it exited
        try:
            p = psutil.Process(state["pid"])
            return abs(p.create_time() - state["create_time"]) < 1.0 and p.status() != psutil.STATUS_ZOMBIE
        except (psutil.Error, KeyError):
            return False

    def status(self, name: str) -> Optional[dict]:
        """Same shape as a docker container entry: {id, state, status, image}; None if never started."""
        state = self._read_state(name)
        if state is None:
            return None
        _, log_file, exit_file = self._paths(name)
        self._maybe_rotate_log(log_file)
        if self._alive(state):
            return {"id": str(state["pid"]), "state": "running", "image": "",
                    "status": f"Up {_ago(state['started_at'])} (pid {state['pid']})"}
        code = None
        if exit_file.exists():
            try:
                code = int(exit_file.read_text().strip())
            except ValueError:
                pass
        if state.get("stopped"):
            text = f"Stopped {_ago(state['stopped_at'])} ago"
        elif code is not None:
            text = f"Exited ({code}) {_ago(exit_file.stat().st_mtime)} ago"
        else:
            text = "Exited"
        return {"id": str(state["pid"]), "state": "exited", "status": text, "image": "", "exit_code": code,
                "failed": code not in (None, 0) and not state.get("stopped")}

    def cleanup_state(self, name: str) -> None:
        """Remove the on-disk state of a deleted recipe: its state/log/exit files, the previous log
        generation and the stop marker. The caller must ensure no other recipe uses this container name
        (the files are keyed by name). Missing files are fine."""
        state_file, log_file, exit_file = self._paths(name)
        for p in (state_file, log_file, log_file.with_suffix(".log.1"), exit_file, self.dir / f"{name}.stopped"):
            p.unlink(missing_ok=True)

    def start(self, name: str, argv: list[str], env: dict[str, str], cwd: Optional[str] = None) -> int:
        if not argv:
            raise ProcessError("The recipe has no command")
        exe = argv[0] if os.sep in argv[0] else shutil.which(argv[0], path=env.get("PATH"))
        if not exe:
            raise ProcessError(f"Command not found: {argv[0]} (PATH={env.get('PATH', '')}). "
                               "Use an absolute path or add its directory to PATH.")
        state_file, log_file, exit_file = self._paths(name)
        if log_file.exists():
            log_file.replace(log_file.with_suffix(".log.1"))
        exit_file.unlink(missing_ok=True)
        bash = shutil.which("bash") or "/bin/bash"
        # Append mode is required: _maybe_rotate_log truncates this file while the child keeps writing.
        with open(log_file, "ab") as log:
            log.write(f"== {datetime.now().astimezone().isoformat(timespec='seconds')} starting: "
                      f"{' '.join(argv)}\n".encode())
            log.flush()
            proc = subprocess.Popen(
                [bash, "-c", WRAPPER, str(exit_file), exe, *argv[1:]],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=env, cwd=cwd or os.path.expanduser("~"), start_new_session=True,
            )
        self._children[name] = proc
        try:
            create_time = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            create_time = 0.0  # already gone: status() then reports the exit code
        state = {"name": name, "pid": proc.pid, "create_time": create_time,
                 "argv": argv, "started_at": time.time(), "stopped": False}
        state_file.write_text(json.dumps(state))
        return proc.pid

    async def stop(self, name: str, timeout: int = config.STOP_TIMEOUT) -> bool:
        """SIGTERM the process group, SIGKILL whatever is left after `timeout`. False if not running."""
        state = self._read_state(name)
        if state is None or not self._alive(state):
            return False
        try:
            root = psutil.Process(state["pid"])
            procs = [root, *root.children(recursive=True)]
        except psutil.Error:
            procs = []
        try:
            os.killpg(state["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        _, alive = await asyncio.to_thread(psutil.wait_procs, procs, timeout)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        if alive:
            await asyncio.to_thread(psutil.wait_procs, alive, 5)
        child = self._children.pop(name, None)
        if child is not None:
            child.poll()
        state.update(stopped=True, stopped_at=time.time())
        self._paths(name)[0].write_text(json.dumps(state))
        return True

    def duplicates(self, name: str, argv: list[str]) -> list[int]:
        """PIDs of processes running `argv` that this manager did not start as `name`.

        Matches the program's base name followed by the same arguments anywhere in a command line,
        so `python3 ~/.local/bin/hermes gateway run` counts as a copy of `hermes gateway run`.
        """
        if not argv:
            return []
        head, rest = os.path.basename(argv[0]), argv[1:]
        ours = {os.getpid()}
        state = self._read_state(name)
        if state is not None and self._alive(state):
            try:
                root = psutil.Process(state["pid"])
                ours |= {root.pid, *(p.pid for p in root.children(recursive=True))}
            except psutil.Error:
                pass
        found = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            cmd = p.info.get("cmdline") or []
            if p.info["pid"] in ours:
                continue
            if any(os.path.basename(tok) == head and cmd[i + 1:i + 1 + len(rest)] == rest
                   for i, tok in enumerate(cmd)):
                found.append(p.info["pid"])
        return sorted(found)

    def logs(self, name: str, tail: int = 500) -> Optional[str]:
        _, log_file, _ = self._paths(name)
        if not log_file.exists():
            return None
        return "\n".join(_tail_lines(log_file, tail)) + "\n"

    def _maybe_rotate_log(self, log_file: Path) -> None:
        """copytruncate: once the log passes LOG_MAX_BYTES, copy it to <name>.log.1 (replacing the old
        generation) and truncate it in place. Safe while the process runs because start() opened the log
        in append mode: the writer's next write lands at the new end, not at a stale offset."""
        try:
            if log_file.stat().st_size <= LOG_MAX_BYTES:
                return
            with open(log_file, "rb") as src, open(log_file.with_suffix(".log.1"), "wb") as dst:
                shutil.copyfileobj(src, dst)
            with open(log_file, "r+b") as f:
                f.truncate(0)
        except OSError:
            pass  # a log that cannot be rotated must not break status/log reads

    async def follow_logs(self, name: str, tail: int = 200) -> AsyncIterator[str]:
        """Yield the last `tail` lines, then new lines until the process has exited."""
        _, log_file, _ = self._paths(name)
        if not log_file.exists():
            return
        with open(log_file, "rb") as f:
            for line in f.read().decode(errors="replace").splitlines()[-tail:] if tail else []:
                yield line
            buf = b""
            idle = 0
            while True:
                chunk = f.read()
                if chunk:
                    idle = 0
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        yield line.decode(errors="replace")
                    continue
                state = self._read_state(name)
                if state is None or not self._alive(state):
                    idle += 1
                    if idle > 2:  # let the last output land
                        if buf:
                            yield buf.decode(errors="replace")
                        return
                await asyncio.sleep(0.5)


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s} seconds"
    if s < 3600:
        return f"{s // 60} minutes"
    if s < 86400:
        return f"{s // 3600} hours"
    return f"{s // 86400} days"
