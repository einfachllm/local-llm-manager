"""Framework presets and translation of a recipe into a `docker run` or native process command."""

import os
import re
import shlex
from typing import Any, Optional

from . import config

# Defaults offered by the UI when a framework is picked. Every value stays editable per recipe.
# In `command`, these placeholders are substituted: {model}, {port} (container port; for native
# processes the same as the host port), {host_port}, {name} (container / process name) and
# {bind_host} (listen address, see bind_host()). {weights} - in model, volumes, command, args and env - is the
# path of the recipe's registered weights on its host (see fill_weights()).
# `runtime` is "docker" (run in a container) or "process" (run directly on the host, e.g. TensorFold).
SPARK_DOCKER_ARGS = "--ipc=host --ulimit memlock=-1 --ulimit stack=67108864"
HF_CACHE_VOLUME = "~/.cache/huggingface:/root/.cache/huggingface"

FRAMEWORKS: dict[str, dict[str, Any]] = {
    "vllm": {
        "runtime": "docker",
        "label": "vLLM",
        "image": "nvcr.io/nvidia/vllm:25.11-py3",
        "container_port": 8000,
        "command": "vllm serve {model} --host {bind_host} --port {port}",
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
        "command": "python3 -m sglang.launch_server --model-path {model} --host {bind_host} --port {port}",
        "args": "--mem-fraction-static 0.4\n--context-length 32768",
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
        "command": "-m {model} --host {bind_host} --port {port}",
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
    "tensorfold": {
        "runtime": "process",
        "label": "TensorFold",
        "image": "",
        "container_port": 8080,
        "command": "tensorfold serve {model} --host {bind_host} --port {port}",
        "args": "",
        "health_path": "/health",
        "docker_args": "",
        "volumes": "",
        "env": "",
    },
    "llamacpp-native": {
        "runtime": "process",
        "label": "llama.cpp server (native)",
        "image": "",
        "container_port": 8080,
        "command": "llama-server -m {model} --host {bind_host} --port {port}",
        "args": "-ngl 999\n-c 32768",
        "health_path": "/health",
        "docker_args": "",
        "volumes": "",
        "env": "",
    },
    "mlx": {
        "runtime": "process",
        "label": "MLX (native, Apple silicon)",
        "image": "",
        "container_port": 8080,
        "command": "mlx_lm.server --model {model} --host {bind_host} --port {port}",
        "args": "",
        "health_path": "/v1/models",
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

# What a recipe runs: an LLM server, or an agent that talks to one (e.g. a Hermes gateway). Agents
# are shown apart on the dashboard, have no endpoint or engine metrics, and "switch" leaves them alone.
KINDS = {"model": "Model", "agent": "Agent"}

# Card colours offered in the UI (purely visual). "" is the default card.
CARD_COLORS = ("", "green", "teal", "blue", "purple", "pink", "orange", "yellow", "red", "black")

MANAGED_LABEL = "local-llm-manager"


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


def bind_host(recipe: dict[str, Any]) -> str:
    """Address the server should listen on: inside its own container network 0.0.0.0 (the published port
    decides who can reach it); on the host itself (native process, --network host) DGXM_PUBLISH_IP if set."""
    on_host = recipe.get("runtime") == "process" or uses_host_network(split_args(recipe.get("docker_args", "")))
    return (config.PUBLISH_IP or "0.0.0.0") if on_host else "0.0.0.0"


def substitute(template: str, recipe: dict[str, Any]) -> str:
    # Plain replacement instead of str.format so JSON arguments with braces stay intact.
    return (
        template.replace("{model}", shlex.quote(recipe.get("model") or ""))
        .replace("{bind_host}", bind_host(recipe))
        .replace("{port}", str(recipe.get("container_port") or ""))
        .replace("{host_port}", str(recipe.get("host_port") or ""))
        .replace("{name}", recipe.get("container_name") or "")
    )


# Stands for the path of the recipe's registered weights on the host it runs on (Weights tab).
WEIGHTS = "{weights}"
WEIGHT_FIELDS = ("model", "volumes", "command", "args", "env")


def uses_weights(recipe: dict[str, Any]) -> bool:
    return any(WEIGHTS in (recipe.get(f) or "") for f in WEIGHT_FIELDS)


def fill_weights(recipe: dict[str, Any], path: Optional[str]) -> dict[str, Any]:
    """A copy of the recipe with {weights} replaced by this host's path (left as is when there is none)."""
    if not path or not uses_weights(recipe):
        return recipe
    return {**recipe, **{f: (recipe.get(f) or "").replace(WEIGHTS, path) for f in WEIGHT_FIELDS}}


def model_mount(model: str, volumes: str) -> Optional[tuple[int, str]]:
    """(line index, host path) of the volume line that mounts exactly the recipe's model path, e.g.
    `~/models/x:/model:ro` for model `/model`; None when the model is not a mount of its own."""
    target = (model or "").strip().rstrip("/")
    if not target.startswith("/"):
        return None
    for i, line in enumerate((volumes or "").splitlines()):
        parts = line.strip().split(":")
        if len(parts) >= 2 and not line.strip().startswith("#") and parts[1].strip().rstrip("/") == target:
            return i, parts[0].strip()
    return None


def use_weights_mount(volumes: str, index: int) -> str:
    """The volumes text with the host side of line `index` replaced by {weights}."""
    lines = volumes.splitlines()
    parts = lines[index].strip().split(":")
    lines[index] = ":".join([WEIGHTS] + parts[1:])
    return "\n".join(lines)


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
        bind = f"{config.PUBLISH_IP}:" if config.PUBLISH_IP else ""
        argv += ["-p", f"{bind}{recipe['host_port']}:{recipe['container_port']}"]
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


def secret_names(recipe: dict[str, Any]) -> list[str]:
    """Bare KEY lines of the env field: variables passed through from the manager's own environment."""
    return [line for line in _lines(recipe.get("env", "")) if "=" not in line]


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


# ---- quick recipes: an existing recipe's (or a preset's) settings + other weights -----------------

# Flags that set the context length, and the one a quick recipe adds when the base has none.
CONTEXT_FLAGS = ("--max-model-len", "--context-length", "-c", "--ctx-size")
CONTEXT_FLAG_FOR = {"vllm": "--max-model-len", "sglang": "--context-length", "llamacpp": "-c", "llamacpp-native": "-c"}
# The model name clients send; kept stable per recipe so agents can address it.
SERVED_NAME_FLAGS = ("--served-model-name", "--alias")
SERVED_NAME_FLAG_FOR = {"vllm": "--served-model-name", "sglang": "--served-model-name",
                        "llamacpp": "--alias", "llamacpp-native": "--alias"}
# Flags that belong to one model, not to the engine: copied from a base recipe, they are worth a look.
MODEL_SPECIFIC_FLAGS = ("--quantization", "--hf-overrides", "--tool-call-parser", "--reasoning-parser",
                        "--speculative-config", "--chat-template", "--tokenizer", "--mamba-cache-mode",
                        "--chat-template-file", "--mmproj")


def slug(text: str) -> str:
    """A short lowercase name usable as container name and served model name."""
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", text).strip("-._").lower() or "model"


def _flag_line(lines: list[str], names: tuple[str, ...]) -> Optional[tuple[int, list[str], int]]:
    """(line index, tokens, token index) of the first of `names` in a multi-line args text."""
    for i, line in enumerate(lines):
        stripped = line.strip().rstrip("\\").strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        for j, t in enumerate(tokens):
            if t in names or any(t.startswith(n + "=") for n in names if n.startswith("--")):
                return i, tokens, j
    return None


def has_flag(args: str, names: tuple[str, ...]) -> bool:
    return _flag_line((args or "").splitlines(), names) is not None


def set_flag(args: str, names: tuple[str, ...], value: str, add_as: Optional[str]) -> tuple[str, bool]:
    """Args with the value of the first of `names` replaced, or `add_as value` appended when none is
    there (and add_as is given). Returns (args, changed)."""
    lines = (args or "").splitlines()
    found = _flag_line(lines, names)
    if found:
        i, tokens, j = found
        if "=" in tokens[j] and tokens[j].startswith("--"):
            tokens[j] = f"{tokens[j].split('=', 1)[0]}={value}"
        elif j + 1 < len(tokens):
            tokens[j + 1] = value
        else:
            tokens.append(value)
        lines[i] = shlex.join(tokens)
        return "\n".join(lines), True
    if not add_as:
        return args or "", False
    lines = [line for line in lines if line.strip()] + [f"{add_as} {shlex.quote(value)}"]
    return "\n".join(lines), True


def quick_recipe(base: dict[str, Any], *, name: str, weights: bool, model: str = "",
                 context: Optional[int] = None) -> tuple[dict[str, Any], list[str]]:
    """A new recipe from `base` (a recipe or a preset, as a recipe dict) that serves other weights:
    with `weights` the recipe reads the registered weights ({weights}), otherwise `model` (a Hugging Face
    id or a path on the host). Sets the served model name and, if given, the context length.
    Returns (recipe, notes for the user)."""
    r = {k: base.get(k) for k in ("runtime", "framework", "image", "command", "args", "env", "volumes",
                                  "docker_args", "gpus", "health_path", "container_port")}
    r.update(name=name, kind="model", model=base.get("model") or "", weights_id=None, est_memory_gb=None,
             tags=[], color="", pinned=False, description="")
    notes: list[str] = []
    docker = r["runtime"] == "docker"
    mount = model_mount(r["model"], r["volumes"] or "") if docker else None
    if weights:
        if not uses_weights(r):
            if not docker:
                r["model"] = WEIGHTS
            elif mount:
                r["volumes"] = use_weights_mount(r["volumes"], mount[0])
            else:
                r["model"] = "/model"
                r["volumes"] = "\n".join([f"{WEIGHTS}:/model:ro"] + [v for v in (r["volumes"] or "").splitlines()])
    else:
        if docker and mount:
            # The base's own model folder is not needed any more.
            lines = r["volumes"].splitlines()
            r["volumes"] = "\n".join(lines[:mount[0]] + lines[mount[0] + 1:])
        if uses_weights(r) and WEIGHTS not in r["model"]:
            raise ValueError("The settings use {weights} in their command or volumes: pick registered weights "
                             "instead of a Hugging Face id")
        r["model"] = model
        if docker and "/" in model and not model.startswith(("/", "~")) and "huggingface" not in (r["volumes"] or ""):
            notes.append("No Hugging Face cache volume: the weights are downloaded again on every start.")
    served = slug(name)
    r["args"], _ = set_flag(r["args"], SERVED_NAME_FLAGS, served, SERVED_NAME_FLAG_FOR.get(r["framework"]))
    if context:
        r["args"], ok = set_flag(r["args"], CONTEXT_FLAGS, str(context), CONTEXT_FLAG_FOR.get(r["framework"]))
        if not ok:
            notes.append("No context-length flag known for this engine: set it in the arguments yourself.")
    copied = [f for f in MODEL_SPECIFIC_FLAGS if has_flag(r["args"], (f,))]
    if copied and base.get("id"):
        notes.append(f"Copied from “{base.get('name')}”, check they fit the new model: {', '.join(copied)}")
    return r, notes
