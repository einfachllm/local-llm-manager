"""Turn a pasted starter script or `docker run` command into a recipe draft.

Rather than parsing bash (defaults, conditionals, arrays, computed JSON), the text is executed by
bash with a PATH holding only a few read-only text tools and a fake `docker` that records the argv
of `docker run`; every other command is recorded and skipped. The argv is then split into the
recipe fields.
"""

import asyncio
import os
import re
import shlex
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any, Optional

from .recipes import FRAMEWORKS

RUN_TIMEOUT = 20

# The only external programs the script may really run during import: read-only text tools that
# starter scripts use to compute values. Anything else found by PATH lookup is recorded and skipped
# by bash's command_not_found_handle (it returns 0, so `set -e` scripts keep going). `docker` is a
# fake that records `docker run`. Programs called by absolute path bypass this, which is why the
# static check (script_check.py) flags them before import.
ALLOWED_TOOLS = (
    "awk", "gawk", "mawk", "sed", "grep", "egrep", "fgrep", "cut", "tr", "head", "tail", "wc",
    "sort", "uniq", "basename", "dirname", "realpath", "readlink", "date", "printf", "echo",
    "cat", "seq", "expr", "bc", "id", "whoami", "hostname", "uname", "nproc", "getconf", "true",
    "false", "test", "[", "bash", "numfmt", "rev", "paste", "column",
)

DOCKER_SHIM = """#!{bash}
if [ "$1" = "run" ]; then
  shift
  : > "$DGXM_CAPTURE"
  for a in "$@"; do printf '%s\\0' "$a" >> "$DGXM_CAPTURE"; done
else
  printf '%s\\0' docker "$@" >> "$DGXM_SKIPPED"; printf '\\036' >> "$DGXM_SKIPPED"
fi
exit 0
"""

# Loaded by every bash started for the import (BASH_ENV), including nested `bash -c`.
PRELUDE = """command_not_found_handle() {
  printf '%s\\0' "$@" >> "$DGXM_SKIPPED"; printf '\\036' >> "$DGXM_SKIPPED"
  return 0
}
"""

# docker run options that take a value (the rest of the options are boolean flags).
VALUE_OPTS = {
    "-e", "--env", "-v", "--volume", "-p", "--publish", "--name", "--gpus", "--entrypoint",
    "--network", "--net", "--ipc", "--shm-size", "--ulimit", "--security-opt", "--env-file",
    "--mount", "-w", "--workdir", "-u", "--user", "-l", "--label", "--device", "--cpus", "-m",
    "--memory", "--memory-swap", "--runtime", "--restart", "-h", "--hostname", "--add-host",
    "--cap-add", "--cap-drop", "--group-add", "--pid", "--uts", "--platform", "--pull",
    "--log-driver", "--log-opt", "--tmpfs", "--cpuset-cpus", "--stop-signal", "--stop-timeout",
    "--health-cmd", "--health-interval", "--health-timeout", "--health-retries", "--dns",
    "--expose", "--label-file", "--cgroupns", "--userns", "--storage-opt", "--sysctl",
}
# Options the manager controls itself.
DROPPED_FLAGS = {"-d", "--detach", "--rm", "-i", "-t", "-it", "-ti", "--interactive", "--tty"}

MODEL_FLAGS = ("--model", "--model-path")


class ScriptImportError(Exception):
    pass


async def capture_docker_run(script: str) -> tuple[list[str], str, list[str]]:
    """Run the script with only ALLOWED_TOOLS available.

    Returns (argv after `docker run`, script output, skipped command lines).
    """
    bash = shutil.which("bash") or "/bin/bash"
    with tempfile.TemporaryDirectory(prefix="dgxm-import-") as tmp:
        tmpdir = Path(tmp)
        bindir = tmpdir / "bin"
        bindir.mkdir()
        for tool in ALLOWED_TOOLS:
            real = shutil.which(tool)
            if real:
                (bindir / tool).symlink_to(real)
        shim = bindir / "docker"
        shim.write_text(DOCKER_SHIM.format(bash=bash))
        shim.chmod(0o755)
        prelude = tmpdir / "prelude.sh"
        prelude.write_text(PRELUDE)
        script_path = tmpdir / "script.sh"
        script_path.write_text(script)
        capture = tmpdir / "capture"
        skipped_log = tmpdir / "skipped"
        work = tmpdir / "work"
        work.mkdir()
        env = {
            "PATH": str(bindir),
            "HOME": os.path.expanduser("~"),
            "USER": os.environ.get("USER", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "BASH_ENV": str(prelude),
            "DGXM_CAPTURE": str(capture),
            "DGXM_SKIPPED": str(skipped_log),
        }
        proc = await asyncio.create_subprocess_exec(
            bash, str(script_path), cwd=work, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            out = b""
        output = out.decode(errors="replace")[-4000:]
        skipped = []
        if skipped_log.exists():
            # Records are separated by \x1e, arguments by NUL.
            for record in skipped_log.read_bytes().split(b"\x1e"):
                args = [a.decode(errors="replace") for a in record.split(b"\0")[:-1]]
                line = shlex.join(args)[:300] if args else ""
                if line and line not in skipped:
                    skipped.append(line)
        if not capture.exists():
            raise ScriptImportError(
                "No `docker run` was executed by the script. Paste a script or command that starts "
                "the model with `docker run`."
                + (f"\n\nSkipped commands:\n" + "\n".join(skipped[:20]) if skipped else "")
                + (f"\n\nScript output:\n{output.strip()}" if output.strip() else "")
            )
        raw = capture.read_bytes().split(b"\0")
        return [a.decode(errors="replace") for a in raw[:-1]], output, skipped


def _tildify(path: str) -> str:
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def _split_opt(tok: str) -> tuple[str, Optional[str]]:
    if tok.startswith("--") and "=" in tok:
        k, v = tok.split("=", 1)
        return k, v
    return tok, None


def _detect_framework(image: str, entrypoint: str, program: list[str]) -> str:
    hay = " ".join([image, entrypoint, *program]).lower()
    if "sglang" in hay:
        return "sglang"
    if "trtllm" in hay or "tensorrt" in hay:
        return "trtllm"
    if "llama.cpp" in hay or "llama-server" in hay or "llama_cpp" in hay:
        return "llamacpp"
    if "vllm" in hay:
        return "vllm"
    return "custom"


def _group_flags(tokens: list[str]) -> list[str]:
    """One line per flag with its values, shell-quoted."""
    lines: list[list[str]] = []
    for tok in tokens:
        if not lines or (tok.startswith("-") and not re.fullmatch(r"-\d+(\.\d+)?", tok)):
            lines.append([tok])
        else:
            lines[-1].append(tok)
    return [shlex.join(line) for line in lines]


def recipe_from_argv(argv: list[str], description: str = "") -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    env, volumes, docker_args, dropped = [], [], [], []
    name = gpus = entrypoint = ""
    publish: list[str] = []
    host_network = False

    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        key, val = _split_opt(argv[i])
        if key in VALUE_OPTS and val is None:
            val = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            i += 1
        if key in DROPPED_FLAGS:
            dropped.append(key)
        elif key in ("-e", "--env"):
            # KEY= (empty) becomes a bare KEY, i.e. passed through from the manager's environment.
            env.append(val[:-1] if val.endswith("=") else val)
        elif key in ("-v", "--volume"):
            host, sep, rest = val.partition(":")
            volumes.append(_tildify(host) + sep + rest)
        elif key in ("-p", "--publish"):
            publish.append(val)
        elif key == "--name":
            name = val
        elif key == "--gpus":
            gpus = val
        else:
            if key == "--entrypoint":
                entrypoint = val
            if key in ("--network", "--net") and val == "host":
                host_network = True
            docker_args += [key, val] if val is not None else [key]
    if i >= len(argv):
        raise ScriptImportError("Could not find the image in the docker run command")
    image = argv[i]
    tokens = argv[i + 1:]

    # Leading positional part of the container command, e.g. `serve /model`, `python3 -m sglang.launch_server`.
    program: list[str] = []
    j = 0
    while j < len(tokens):
        t = tokens[j]
        if t == "-m" and program and os.path.basename(program[-1]).startswith("python") and j + 1 < len(tokens):
            program += tokens[j:j + 2]
            j += 2
        elif t.startswith("-"):
            break
        else:
            program.append(t)
            j += 1
    rest = tokens[j:]
    framework = _detect_framework(image, entrypoint, program)

    model, model_flag = "", ""
    for k, t in enumerate(program):
        if k > 0 and program[k - 1].endswith("serve"):
            model = t
            program[k] = "{model}"
            break

    host, port = "", ""
    remaining: list[str] = []
    k = 0
    while k < len(rest):
        key, val = _split_opt(rest[k])
        is_model_flag = key in MODEL_FLAGS or (key == "-m" and framework == "llamacpp")
        if (key in ("--host", "--port") or (is_model_flag and not model)) and (val is not None or k + 1 < len(rest)):
            if val is None:
                val = rest[k + 1]
                k += 2
            else:
                k += 1
            if key == "--host":
                host = val
            elif key == "--port":
                port = val
            else:
                model, model_flag = val, key
            continue
        remaining.append(rest[k])
        k += 1

    command = list(program)
    if model_flag:
        command += [model_flag, "{model}"]
    if host:
        command += ["--host", host]
    if port:
        command += ["--port", "{port}"]
    # shlex.join would quote the placeholders; they are plain words, so join manually.
    command_str = " ".join(t if t in ("{model}", "{port}") else shlex.quote(t) for t in command)

    preset = FRAMEWORKS[framework]
    container_port = int(port) if port.isdigit() else None
    host_port = None
    if publish:
        parts = publish[0].split(":")
        if len(parts) >= 2 and parts[-2].isdigit():
            host_port = int(parts[-2])
        cport = parts[-1].split("/")[0]
        if cport.isdigit():
            container_port = container_port or int(cport)
        if len(publish) > 1:
            warnings.append(f"Only the first port mapping is used; ignored: {', '.join(publish[1:])}")
    container_port = container_port or preset["container_port"]
    host_port = host_port or container_port
    if host_network and publish:
        warnings.append("--network host is set, so the -p mapping has no effect")

    if dropped:
        warnings.append(f"Removed {' '.join(sorted(set(dropped)))} (the manager runs containers detached itself)")
    if not model:
        warnings.append("Could not tell which argument is the model; check the Model and Command fields")

    served = ""
    for k, t in enumerate(remaining):
        if t == "--served-model-name" and k + 1 < len(remaining):
            served = remaining[k + 1]
    display_name = served or name or (os.path.basename(model.rstrip("/")) if model else "") or "Imported recipe"

    recipe = {
        "name": display_name,
        "description": description,
        "framework": framework,
        "image": image,
        "model": model,
        "container_name": name,
        "host_port": host_port,
        "container_port": container_port,
        "command": command_str,
        "args": "\n".join(_group_flags(remaining)),
        "env": "\n".join(env),
        "volumes": "\n".join(volumes),
        "docker_args": shlex.join(docker_args),
        "gpus": gpus,
        "health_path": preset["health_path"],
        "est_memory_gb": None,
        "tags": [],
        "color": "",
    }
    return recipe, warnings


def description_from_script(script: str) -> str:
    """First meaningful comment line of the script, e.g. '# Starter: org/model on DGX Spark'."""
    for line in script.splitlines()[:30]:
        s = line.strip()
        if s.startswith("#!"):
            continue
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            if len(text) > 3 and not text.startswith(("./", "-", "*")):
                return text[:200]
        elif s:
            break
    return ""


async def import_script(script: str) -> dict[str, Any]:
    argv, output, skipped = await capture_docker_run(script)
    recipe, warnings = recipe_from_argv(argv, description_from_script(script))
    return {"recipe": recipe, "warnings": warnings, "docker_argv": argv, "skipped_calls": skipped}
