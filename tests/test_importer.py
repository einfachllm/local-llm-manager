import asyncio
import os
from pathlib import Path

from dgxspark_manager.importer import import_script, recipe_from_argv
from dgxspark_manager.recipes import build_run_argv

FIXTURES = Path(__file__).parent / "fixtures"


def run_import(text):
    return asyncio.run(import_script(text))


def test_starter_script_round_trip():
    text = (FIXTURES / "starter-qwen38-flash-next-fp4.sh").read_text()
    res = run_import(text)
    r = res["recipe"]
    assert r["framework"] == "vllm"
    assert r["image"] == "eugr/spark-vllm-b12x:latest"
    assert r["model"] == "/model"
    assert r["name"] == "qwen38-flash-next-fp4"
    assert r["container_name"] == "qwen38-flash-next"
    assert (r["host_port"], r["container_port"]) == (8081, 8081)
    assert r["command"] == "serve {model} --host 0.0.0.0 --port {port}"
    assert r["description"].startswith("Starter: nvidia/Qwen3.8-Flash-Next-FP4-Local")
    assert "--entrypoint vllm" in r["docker_args"] and "--network host" in r["docker_args"]
    assert "~/models/Qwen3.8-Flash-Next-FP4-Local:/model:ro" in r["volumes"].splitlines()
    assert "CUTE_DSL_ARCH=sm_121a" in r["env"].splitlines()
    assert any("--rm" in w for w in res["warnings"])

    # Computed values (yarn factor, MTP config) are resolved by running the script.
    assert '"factor":2,' in r["args"]
    assert "--speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":4}'" in r["args"].splitlines()

    # Rebuilding gives exactly the container command of the script, and no -p with host networking.
    original = res["docker_argv"]
    rebuilt = build_run_argv({**r, "id": 1})
    img = r["image"]
    assert rebuilt[rebuilt.index(img) + 1:] == original[original.index(img) + 1:]
    assert "-p" not in rebuilt
    opts = lambda argv: sorted(a for a in argv[:argv.index(img)] if a not in ("--rm", "-d", "-p"))
    extra = {"docker", "run", "--label", "dgxspark-manager=1", "dgxspark-manager.recipe=1"}
    assert [a for a in opts(rebuilt) if a not in extra] == [os.path.expanduser(a) for a in opts(original)]


def test_plain_docker_run_command():
    text = """docker run -d --rm --gpus all -p 8200:8000 \\
      -e HF_TOKEN="" -e FOO=bar -v $HOME/.cache/huggingface:/root/.cache/huggingface \\
      --ipc=host vllm/vllm-openai:latest --model Qwen/Qwen3-8B --port 8000 --max-model-len=8192 \\
      --override-generation-config '{"temperature": 0.6}'"""
    res = run_import(text)
    r = res["recipe"]
    assert r["framework"] == "vllm" and r["model"] == "Qwen/Qwen3-8B"
    assert r["command"] == "--model {model} --port {port}"
    assert (r["host_port"], r["container_port"]) == (8200, 8000)
    assert r["env"].splitlines() == ["HF_TOKEN", "FOO=bar"]  # empty value -> pass-through
    assert r["volumes"] == "~/.cache/huggingface:/root/.cache/huggingface"
    assert r["args"].splitlines() == [
        "--max-model-len=8192", "--override-generation-config '{\"temperature\": 0.6}'"]
    assert r["name"] == "Qwen3-8B"


def test_sglang_python_module():
    r, _ = recipe_from_argv([
        "--gpus", "all", "lmsysorg/sglang:spark", "python3", "-m", "sglang.launch_server",
        "--model-path", "Qwen/Qwen3-8B", "--host", "0.0.0.0", "--port", "30000", "--tp", "1",
    ])
    assert r["framework"] == "sglang"
    assert r["command"] == "python3 -m sglang.launch_server --model-path {model} --host 0.0.0.0 --port {port}"
    assert r["args"] == "--tp 1"
    assert (r["host_port"], r["container_port"]) == (30000, 30000)


def test_no_docker_run(client):
    res = client.post("/api/recipes/import", json={"text": "echo hello"})
    assert res.status_code == 422
    assert "neither ran `docker run`" in res.json()["detail"] and "hello" in res.json()["detail"]


def test_import_endpoint_avoids_name_clashes(client):
    text = "docker run --name m1 -p 9000:8000 vllm/vllm-openai --model org/m1"
    first = client.post("/api/recipes/import", json={"text": text}).json()["recipe"]
    assert client.post("/api/recipes", json=first).status_code == 201
    second = client.post("/api/recipes/import", json={"text": text}).json()
    assert second["recipe"]["name"] == "m1 (2)" and second["recipe"]["container_name"] == "m1-2"
    assert client.post("/api/recipes", json=second["recipe"]).status_code == 201


def test_native_mlx_script():
    text = """#!/usr/bin/env bash
# Starter: Qwen3 on the Mac Studio with MLX
MODEL="${MODEL:-mlx-community/Qwen3-30B-A3B-4bit}"
PORT="${PORT:-8085}"
source ~/venvs/mlx/bin/activate
exec mlx_lm.server --model "$MODEL" --host 0.0.0.0 --port "$PORT" --max-tokens 4096 --log-level INFO
"""
    res = run_import(text)
    r = res["recipe"]
    assert r["runtime"] == "process" and r["framework"] == "mlx"
    assert r["model"] == "mlx-community/Qwen3-30B-A3B-4bit"
    assert r["command"] == "mlx_lm.server --model {model} --host 0.0.0.0 --port {port}"
    assert (r["host_port"], r["container_port"]) == (8085, 8085)
    assert r["args"].splitlines() == ["--max-tokens 4096", "--log-level INFO"]
    assert r["description"] == "Starter: Qwen3 on the Mac Studio with MLX"
    assert any("native process" in w for w in res["warnings"])


def test_native_llama_server_and_python_module():
    r = run_import("llama-server -m ~/models/q.gguf --port 8090 -ngl 999 -c 8192")["recipe"]
    assert r["framework"] == "llamacpp-native" and r["model"] == "~/models/q.gguf"
    assert r["command"] == "llama-server -m {model} --port {port}" and r["args"].splitlines() == ["-ngl 999", "-c 8192"]

    r = run_import("python3 -m mlx_lm server --model m/x")["recipe"]
    assert r["framework"] == "mlx" and r["command"] == "python3 -m mlx_lm server --model {model}"
    assert r["host_port"] == 8080  # mlx default, no --port given


def test_native_import_saves_as_process_recipe(client):
    res = client.post("/api/recipes/import", json={"text": "mlx_lm.server --model m/y --port 8086"}).json()
    saved = client.post("/api/recipes", json=res["recipe"])
    assert saved.status_code == 201 and saved.json()["runtime"] == "process"
