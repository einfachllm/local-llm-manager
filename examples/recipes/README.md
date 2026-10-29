# Importable recipes

Ready-made recipes you paste into the manager instead of typing fields: open **Recipes → Import**, paste
a whole file from this folder, review the draft (name, port, paths) and create. The first comment line of
a script becomes the recipe's description.

| File | What it is |
|---|---|
| [`qwen38-flash-next-fp4.sh`](qwen38-flash-next-fp4.sh) | Qwen3.8-Flash-Next-FP4 served by vLLM on an NVIDIA DGX Spark (GB10, SM121), tuned flags, tool calling on |

## Before you start

- **Weights:** each script says where it expects the model files; download them there (or point the
  volumes at your own copy). "Search hosts" in the **Weights** tab finds model folders under
  `DGXM_MODEL_DIR` and registers them.
- **Ports:** the port in `--port` becomes the recipe's port. Keep it the **same across models** you want
  to switch between, and give each `--served-model-name` a stable short name - then **Switch to** changes
  the model and your agents keep the same endpoint `http://<host>:<port>/v1`.
- **Secrets:** scripts list only bare names (e.g. `-e HF_TOKEN`); put the values in the manager's `.env`
  (see [docs/configuration.md](../../docs/configuration.md)).
- Scripts are a starting point: the flags are tuned for the machine they ran on. Check
  `--gpu-memory-utilization`, `--max-model-len` and `--max-num-seqs` against your GPU's memory.

More of your own? Drop new `.sh` files here and add a row to the table - same format as
[`../hermes-agent/docker-recipe.example.sh`](../hermes-agent/docker-recipe.example.sh).
