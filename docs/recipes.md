# Recipes

Recipes save how to start a model server (vLLM, SGLang, llama.cpp, MLX, TensorFold or any image / native binary) as a Docker container or a native process. You can also import an existing start script, `docker run` command or compact JSON/YAML recipe file, and export any recipe the same way.

## Importable example recipes

Ready-made recipes you paste into the manager instead of typing fields: click **＋ Add recipe**, paste a whole file from [`examples/recipes`](https://github.com/einfachllm/local-llm-manager/tree/main/examples/recipes), review the draft (name, port, paths) and create.

Two formats are accepted:

- **`.yaml` / `.json` - compact recipe files.** The manager's own fields: image, model, ports, `args`, `env`, `volumes`, `docker_args`, command, health path, tags. Missing settings come from the framework preset, so only what differs is written down. `weights:` names a registered Weights-tab entry (its per-host path fills `{weights}`); omit `model:` for the usual `/model` mount, or set it to another path. `host_port:` is the port on the host (`port:` is shorthand for it). `args`, `env`, `volumes` and `docker_args` can be multi-line strings or lists.
- **`.sh` - starter scripts / `docker run` commands.** The manager checks the script, runs it with bash in a restricted mode and turns the captured `docker run` into the same fields.

The current examples:

| File | What it is |
|---|---|
| [`qwen38-27b.yaml`](https://github.com/einfachllm/local-llm-manager/blob/main/examples/recipes/qwen38-27b.yaml) | Qwen3.8 27B served by vLLM - a small compact recipe with registered weights. |
| [`qwen38-flash-next-fp4.yaml`](https://github.com/einfachllm/local-llm-manager/blob/main/examples/recipes/qwen38-flash-next-fp4.yaml) | Qwen3.8-Flash-Next-FP4 on an NVIDIA DGX Spark (GB10, SM121): b12x image, tuned flags, caches, tool calling. Compact form. |
| [`nemotron-3.5-super-vl.yaml`](https://github.com/einfachllm/local-llm-manager/blob/main/examples/recipes/nemotron-3.5-super-vl.yaml) | Nemotron-3.5-Super-VL with a draft model for speculative decoding; both models sit in one registered folder. |
| [`hermes-agent-docker.yaml`](https://github.com/einfachllm/local-llm-manager/blob/main/examples/recipes/hermes-agent-docker.yaml) | A Hermes Agent gateway as an agent recipe: its own port and data volume, no model or GPU. |
| [`embeddinggemma-2-llamacpp.yaml`](https://github.com/einfachllm/local-llm-manager/blob/main/examples/recipes/embeddinggemma-2-llamacpp.yaml) | EmbeddingGemma 2 (GGUF) as an OpenAI-compatible `/v1/embeddings` server via llama.cpp. |
| [`sglang-flint.sh`](https://github.com/einfachllm/local-llm-manager/blob/main/examples/recipes/sglang-flint.sh) | RecursiveSI/flint served natively by SGLang's `sglang serve` CLI, port 30000. |
| [`sglang-flint-docker.sh`](https://github.com/einfachllm/local-llm-manager/blob/main/examples/recipes/sglang-flint-docker.sh) | The same model in the official `lmsysorg/sglang` Docker image (`python3 -m sglang.launch_server`). |

## Before you start

- **Weights:** compact recipes name a registered entry (`weights:`). Create it in the **Weights** tab with the folder on each host that runs the recipe, or change the name in the file. If a main model and a draft model share a folder, register that folder once, mount it, and point `model:` at the subfolder (see `nemotron-3.5-super-vl.yaml`). Starter scripts mount a concrete folder; download the model there, or point the volume at your copy. The **Weights** tab finds model folders under `DGXM_MODEL_DIR` by itself and offers them under "Found on your hosts".
- **Ports:** the port in `host_port:` / `port:` / `--port` becomes the recipe's port. Keep it the **same across models** you want to switch between, and give each `--served-model-name` a stable short name - then **Switch to** changes the model and your agents keep the same endpoint `http://<host>:<port>/v1`.
- **Secrets:** files list only bare names (e.g. `HF_TOKEN`); put the values in the manager's `.env` (see [Configuration](configuration.md)).
- **vLLM images:** the preset command starts with `vllm serve`. For `vllm/vllm-openai` (whose entrypoint already runs `vllm serve`), the manager drops the duplicate; images with a plain `vllm` entrypoint keep `serve`. The preview shows the actual command.
- Recipes are a starting point: the flags are tuned for the machine they ran on. Check `--gpu-memory-utilization`, `--max-model-len` and `--max-num-seqs` against your GPU's memory.

More of your own? Drop new `.yaml` / `.json` files in `examples/recipes` and add a row to its table - or export one from the editor with **Recipe file…**.
