# Starter: RecursiveSI/flint served natively by SGLang's `sglang serve` CLI (port 30000).
#
# Paste this whole file into + Add recipe and review the draft. This is a native process, not a
# container: install SGLang on the host that runs it (e.g. `pip install "sglang[all]"`, or the
# SGLang wheel for your CUDA version) and make sure `sglang` is on the manager's PATH.
#
# The model is downloaded from Hugging Face on first start (RecursiveSI/flint). Gated models need
# HF_TOKEN in the manager's .env. Tune --tp-size, --mem-fraction-static and --context-length for
# your GPUs; the imported recipe keeps the same port, so "Switch to" can swap it for another model.
sglang serve \
  --model-path RecursiveSI/flint \
  --served-model-name flint \
  --tp-size 1 \
  --enable-fp32-lm-head \
  --context-length 262144 \
  --chunked-prefill-size 4096 \
  --enable-unified-memory \
  --mem-fraction-static 0.8 \
  --weight-loader-drop-cache-after-load \
  --tool-call-parser flint \
  --reasoning-parser flint \
  --host 0.0.0.0 \
  --port 30000
