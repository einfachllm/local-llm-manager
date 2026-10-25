"""Framework presets and translation of a recipe into a `docker run` or native process command."""

import os
import shlex
from typing import Any

from . import config

# Defaults offered by the UI when a framework is picked. Every value stays editable per recipe.
# In `command`, these placeholders are substituted: {model}, {port} (container port; for native
# processes the same as the host port), {host_port}, {name} (container / process name).
# `runtime` is "docker" (run in a container) or "process" (run directly on the host, e.g. MLX on a Mac).
SPARK_DOCKER_ARGS = "--ipc=host --ulimit memlock=-1 --ulimit stack=67108864"
HF_CACHE_VOLUME = "~/.cache/huggingface:/root/.cache/huggingface"

FRAMEWORKS: dict[str, dict[str, Any]] = {
    "vllm": {
        "runtime": "docker",
        "label": "vLLM",
        "image": "nvcr.io/nvidia/vllm:25.11-py3",
        "container_port": 8000,
        "command": "vllm serve {model} --host 0.0.0.0 --port {port}",
        "args": "--gpu-memory-utilization 0.4\n--max-model-len 32768\n--max-num-seqs 8",
        "health_path": "/health",
        "docker_args": SPARK_DOCKER_ARGS,
        "volumes": HF_CACHE_VOLUME,
        "env": "HF_TOKEN",
    },
    "sglang": {
        "runtime": "docker",
        "label": "SGLang",
        "image": "lmsysorg/sglang:spark",
        "container_port": 30000,
        "command": "python3 -m sglang.launch_server --model-path {model} --host 0.0.0.0 --port {port}",
        "args": "--mem-fraction-static 0.4\n--context-length 32768",
        "health_path": "/health",
        "docker_args": SPARK_DOCKER_ARGS,
        "volumes": HF_CACHE_VOLUME,
        "env": "HF_TOKEN",
    },
    "trtllm": {
        "runtime": "docker",
        "label": "TensorRT-LLM",
        "image": "nvcr.io/nvidia/tensorrt-llm/release:latest",
        "container_port": 8000,
        "command": "trtllm-serve {model} --host 0.0.0.0 --port {port}",
        "args": "",
        "health_path": "/health",
        "docker_args": SPARK_DOCKER_ARGS,
        "volumes": HF_CACHE_VOLUME,
        "env": "HF_TOKEN",
    },
    "llamacpp": {
        "runtime": "docker",
        "label": "llama.cpp server",
        "image": "ghcr.io/ggml-org/llama.cpp:server-cuda",
        "container_port": 8080,
        "command": "-m {model} --host 0.0.0.0 --port {port}",
        "args": "-ngl 999\n-c 32768",
        "health_path": "/health",
        "docker_args": "",
        "volumes": "~/models:/models",
        "env": "",
    },
    "custom": {
        "runtime": "docker",
        "label": "Custom (Docker)",
        "image": "",
        "container_port": 8000,
        "command": "",
        "args": "",
        "health_path": "/health",
        "docker_args": SPARK_DOCKER_ARGS,
        "volumes": "",
        "env": "",
    },
    "mlx": {
        "runtime": "process",
        "label": "MLX LM server",
        "image": "",
        "container_port": 8080,
        "command": "mlx_lm.server --model {model} --host 0.0.0.0 --port {port}",
        "args": "",
        "health_path": "/v1/models",
        "docker_args": "",
        "volumes": "",
        "env": "",
    },
    "llamacpp-native": {
        "runtime": "process",
        "label": "llama.cpp server (native)",
        "image": "",
        "container_port": 8080,
        "command": "llama-server -m {model} --host 0.0.0.0 --port {port}",
        "args": "-ngl 999\n-c 32768",
        "health_path": "/health",
        "docker_args": "",
        "volumes": "",
        "env": "",
    },
    "process-custom": {
        "runtime": "process",
        "label": "Custom (native)",
        "image": "",
        "container_port": 8000,
        "command": "",
        "args": "",
        "health_path": "/health",
        "docker_args": "",
        "volumes": "",
        "env": "",
    },
}

RUNTIMES = {"docker": "Docker container", "process": "Native process"}

# Card colours offered in the UI (purely visual). "" is the default card.
CARD_COLORS = ("", "green", "teal", "blue", "purple", "pink", "orange", "yellow", "red", "black")

MANAGED_LABEL = "dgxspark-manager"


def _lines(text: str) -> list[str]:
    """Non-empty, non-comment lines of a multi-line text field."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _expand_home(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def substitute(template: str, recipe: dict[str, Any]) -> str:
    # Plain replacement instead of str.format so JSON arguments with braces stay intact.
    return (
        template.replace("{model}", shlex.quote(recipe.get("model") or ""))
        .replace("{port}", str(recipe.get("container_port") or ""))
        .replace("{host_port}", str(recipe.get("host_port") or ""))
        .replace("{name}", recipe.get("container_name") or "")
    )


def split_args(text: str) -> list[str]:
    """Split a multi-line args field. Lines starting with '#' are comments; trailing '\\' is allowed."""
    joined = " ".join(line.rstrip("\\").strip() for line in _lines(text))
    return shlex.split(joined)


def uses_host_network(docker_args: list[str]) -> bool:
    """With --network host the container listens on the host directly and -p is meaningless."""
    for i, a in enumerate(docker_args):
        if a in ("--network=host", "--net=host"):
            return True
        if a in ("--network", "--net") and i + 1 < len(docker_args) and docker_args[i + 1] == "host":
            return True
    return False


def build_run_argv(recipe: dict[str, Any]) -> list[str]:
    """Build the full `docker run` argv for a recipe."""
    argv = [
        config.DOCKER_BIN, "run", "-d",
        "--name", recipe["container_name"],
        "--label", f"{MANAGED_LABEL}=1",
        "--label", f"{MANAGED_LABEL}.recipe={recipe.get('id', '')}",
    ]
    if recipe.get("gpus"):
        argv += ["--gpus", recipe["gpus"]]
    docker_args = split_args(recipe.get("docker_args", ""))
    if not uses_host_network(docker_args):
        argv += ["-p", f"{recipe['host_port']}:{recipe['container_port']}"]
    for vol in _lines(recipe.get("volumes", "")):
        argv += ["-v", _expand_home(vol)]
    for env in _lines(recipe.get("env", "")):
        # A bare KEY passes the variable through from the manager's own environment.
        argv += ["-e", env]
    argv += docker_args
    argv.append(recipe["image"])
    argv += split_args(substitute(recipe.get("command", ""), recipe))
    argv += split_args(recipe.get("args", ""))
    return argv


def format_command(argv: list[str]) -> str:
    """Human-readable, copy-pasteable rendering of an argv (secrets passed as bare env names stay hidden)."""
    return shlex.join(argv)


def build_process_argv(recipe: dict[str, Any]) -> list[str]:
    """argv for a native-process recipe: command + framework arguments, with `~` expanded."""
    argv = split_args(substitute(recipe.get("command", ""), recipe)) + split_args(recipe.get("args", ""))
    return [os.path.expanduser(a) if a.startswith("~") else a for a in argv]


def process_env(recipe: dict[str, Any]) -> dict[str, str]:
    """Environment for a native process: the manager's own, plus KEY=value lines of the recipe."""
    env = dict(os.environ)
    for line in _lines(recipe.get("env", "")):
        key, sep, value = line.partition("=")
        if sep:
            env[key.strip()] = os.path.expanduser(value)
    return env


def format_process_command(recipe: dict[str, Any]) -> str:
    env = [line for line in _lines(recipe.get("env", "")) if "=" in line]
    return " ".join([shlex.quote(e) for e in env] + [shlex.join(build_process_argv(recipe))])
