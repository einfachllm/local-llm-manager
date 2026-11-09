# Importable recipes

Ready-made recipes you paste into the manager instead of typing fields: click **＋ Add recipe**, paste
a whole file from this folder, review the draft (name, port, paths) and create.

Two formats are accepted:

- **`.yaml` / `.json` - compact recipe files.** The manager's own fields: image, model, ports, `args`,
  `env`, `volumes`, `docker_args`, command, health path, tags. Missing settings come from the framework
  preset, so only what differs is written down. `weights:` names a registered Weights-tab entry (its
  per-host path fills `{weights}`); omit `model:` for the usual `/model` mount, or set it to another path.
  `host_port:` is the port on the host (`port:` is shorthand for it). `args`, `env`, `volumes` and `docker_args` can be multi-line
  strings or lists. Everything a complicated start script needs - bind mounts, environment, extra Docker
  flags, entrypoint, server arguments - has a field, so importing the file gives the same container again.
- **`.sh` - starter scripts / `docker run` commands.** The manager checks the script, runs it with bash in
  a restricted mode (only a fake `docker` and read-only text tools really run) and turns the captured
  `docker run` into the same fields. Use this for scripts that compute values or have preflight checks.

The first comment line of a script becomes the recipe's description. In the recipe editor,
**Recipe file…** exports the current recipe in the compact format, ready for another manager or for this
folder.

| File | Engine | What it is |
|---|---|---|
| [`qwen38-27b.yaml`](qwen38-27b.yaml) | vLLM | Qwen3.8 27B with MTP - the small compact recipe, and the one a fresh install starts with. |
| [`qwen38-dflash.yaml`](qwen38-dflash.yaml) | vLLM | Qwen3.8-27B-NVFP4 with the DFlash2 drafter: speculative decoding, model and drafter from one registered folder. |
| [`qwen38-dflash-docker.sh`](qwen38-dflash-docker.sh) | vLLM | The same recipe as a **starter script**: a plain `docker run`, and the fields the manager reads out of it. |
| [`qwen38-sglang.yaml`](qwen38-sglang.yaml) | SGLang | The same model and drafter on SGLang's official image, **same port** - **Switch to** swaps the engine. |
| [`qwen38-tensorfold.yaml`](qwen38-tensorfold.yaml) | TensorFold | The same model and drafter served natively (no Docker) by TensorFold's CUDA engine on a DGX Spark. |
| [`qwen38-flash-next-fp4.yaml`](qwen38-flash-next-fp4.yaml) | vLLM | Qwen3.8-Flash-Next-FP4 on an NVIDIA DGX Spark (GB10, SM121): b12x image, tuned flags, caches, tool calling. |
| [`qwen38-flash-next-tensorfold.yaml`](qwen38-flash-next-tensorfold.yaml) | TensorFold | The same Flash-Next weights on TensorFold's Zig engine in MiaAI-Lab's starter image: 262k window, 5 streams, fp8 KV, MTP drafts, image and video in. |
| [`nemotron-3.5-super-vl.yaml`](nemotron-3.5-super-vl.yaml) | vLLM | Nemotron-3.5-Super-VL with a draft model for speculative decoding; both models sit in one registered folder. |
| [`hermes-agent-docker.yaml`](hermes-agent-docker.yaml) | - | A Hermes Agent gateway as an agent recipe: its own port and data volume, no model or GPU. |

The four Qwen3.8-27B recipes (`qwen38-27b`, `qwen38-dflash`, `qwen38-sglang`, `qwen38-tensorfold`) are
the same model on four engines: they share port **8101** and the served name `qwen3.8`, so one endpoint
`http://<host>:8101/v1` keeps working while you switch (`qwen38-dflash-docker.sh` is the second one again,
as a starter script). The three with a drafter mount one registered folder (`Qwen3.8-Models` = the folder
holding `Qwen3.8-27B-NVFP4` **and** `Qwen3.8-27B-DFlash2`), which is how a main model and its drafter stay
together on every host. The two `qwen38-flash-next-*` recipes are one model on two engines as well - they
share the weights entry and differ in image and port.

## Before you start

- **Weights:** compact recipes name a registered entry (`weights:`), e.g. `Qwen3.8-Flash-Next-FP4-Local`.
  Create it in the **Weights** tab with the folder on each host that runs the recipe, or change the name in
  the file. If a main model and a draft model share a folder, register that folder once, mount it, and point
  `model:` at the subfolder (see `nemotron-3.5-super-vl.yaml`, `qwen38-dflash.yaml`). Note that a parent
  folder registered for that reason (`Qwen3.8-Models`, `Nemotron-3.5-Models` = `~/models`) is **typed in** -
  "Search hosts" lists the model folders inside `DGXM_MODEL_DIR`, not their parent. Starter scripts mount a
  concrete folder; download the model there, or point the volume at your copy. "Search hosts" in the
  **Weights** tab finds model folders under `DGXM_MODEL_DIR` and registers them.
- **Ports:** the port in `host_port:` / `port:` / `--port` becomes the recipe's port. Keep it the **same across models**
  you want to switch between, and give each `--served-model-name` a stable short name - then **Switch to**
  changes the model and your agents keep the same endpoint `http://<host>:<port>/v1`.
- **Secrets:** files list only bare names (e.g. `HF_TOKEN`); put the values in the manager's `.env` (see
  [docs/configuration.md](../../docs/configuration.md)).
- **vLLM images:** the preset command starts with `vllm serve`. For `vllm/vllm-openai` (whose entrypoint
  already runs `vllm serve`), the manager drops the duplicate; images with a plain `vllm` entrypoint keep
  `serve`. The preview shows the actual command.
- Recipes are a starting point: the flags are tuned for the machine they ran on. Check
  `--gpu-memory-utilization`, `--max-model-len` and `--max-num-seqs` against your GPU's memory.

More of your own? Drop new `.yaml` / `.json` files here and add a row to the table - or export one from
the editor with **Recipe file…**.
