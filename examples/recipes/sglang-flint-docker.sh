# Starter: RecursiveSI/flint in the official SGLang Docker image (port 30000).
#
# Paste this whole file into + Add recipe and review the draft. The official lmsysorg/sglang images
# do not put the `sglang` console script on PATH, so use `python3 -m sglang.launch_server` - with
# `sglang serve` the container exits with "exec: sglang: not found".
#
# HF_TOKEN is passed through from the manager's .env (bare name, no value in the recipe). The model
# is downloaded into the mounted Hugging Face cache on first start. Tune --tp-size, --shm-size,
# --mem-fraction-static and --context-length for your GPUs. For RDMA/multi-node setups, add
# --network host and --privileged as in SGLang's own compose file.
docker run --gpus all \
  --shm-size 32g \
  -p 30000:30000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -e HF_TOKEN \
  --ipc=host \
  lmsysorg/sglang:dev-flint \
  python3 -m sglang.launch_server \
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
