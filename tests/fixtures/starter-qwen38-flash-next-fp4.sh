#!/usr/bin/env bash
#
# Starter: nvidia/Qwen3.8-Flash-Next-FP4-Local on NVIDIA DGX Spark (GB10, SM121)
#
#   ./starter-qwen38-flash-next-fp4.sh                 # foreground
#   DETACH=1 ./starter-qwen38-flash-next-fp4.sh        # background + health wait
#   DRY_RUN=1 ./starter-qwen38-flash-next-fp4.sh       # print docker command
#   SPEC=none ./starter-qwen38-flash-next-fp4.sh       # disable MTP draft head
#   PLE_OFFLOAD=0 ./starter-qwen38-flash-next-fp4.sh   # keep n-gram table in RAM
#
# Env overrides: IMAGE, GPU_MEM_UTIL, MAX_MODEL_LEN, PORT, SERVED_NAME, etc.
# Stop:  docker rm -f qwen38-flash-next
#
# Notes:
#  * This checkpoint's NVFP4 scale factors use a CSF format that upstream vLLM
#    cannot load. You must use a community container with --load-format b12x
#    (default: eugr/spark-vllm-b12x:latest, arm64 for GB10).
#  * Recipe: https://huggingface.co/nvidia/Qwen3.8-Flash-Next-FP4-Local
#  * Keep GPU_MEM_UTIL <= 0.9; the Spark shares unified memory with the OS.
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-$HOME/models/Qwen3.8-Flash-Next-FP4-Local}"
SERVED_NAME="${SERVED_NAME:-qwen38-flash-next-fp4}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8081}"

# Serving profile (model card recipe + community tuning)
IMAGE="${IMAGE:-eugr/spark-vllm-b12x:latest}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.89}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-524288}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-7}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-16384}"
OPTIMIZATION_LEVEL="${OPTIMIZATION_LEVEL:-3}"
PERFORMANCE_MODE="${PERFORMANCE_MODE:-throughput}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"
BLOCK_SIZE="${BLOCK_SIZE:-16}"
TP="${TP:-1}"
PP="${PP:-1}"
LOAD_FORMAT="${LOAD_FORMAT:-b12x}"
LINEAR_BACKEND="${LINEAR_BACKEND:-b12x}"
MOE_BACKEND="${MOE_BACKEND:-b12x}"
GDN_DECODE_KERNEL="${GDN_DECODE_KERNEL:-b12x}"
MAMBA_CACHE_MODE="${MAMBA_CACHE_MODE:-align}"
ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-1}"
ENABLE_CHUNKED_PREFILL="${ENABLE_CHUNKED_PREFILL:-1}"
FUSE_ACT_QUANT="${FUSE_ACT_QUANT:-1}"
PLE_OFFLOAD="${PLE_OFFLOAD:-1}"
REASONING_PARSER="${REASONING_PARSER:-qwen3}"
TOOL_PARSER="${TOOL_PARSER:-qwen3_coder}"
SPEC="${SPEC:-mtp}"
SPEC_TOKENS="${SPEC_TOKENS:-4}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

# yarn rope scaling to stretch native 262144 to MAX_MODEL_LEN
# (factor = MAX_MODEL_LEN / 262144, e.g. 2.0 for the 524288 default;
#  no override is injected when MAX_MODEL_LEN <= 262144)
HF_OVERRIDES="${HF_OVERRIDES-}"
if [ -z "$HF_OVERRIDES" ] && [ "$MAX_MODEL_LEN" -gt 262144 ] 2>/dev/null; then
  ROPE_FACTOR=$(awk -v m="$MAX_MODEL_LEN" 'BEGIN{printf "%.4g", m/262144}')
  HF_OVERRIDES='{"max_position_embeddings":'"$MAX_MODEL_LEN"',"text_config":{"max_position_embeddings":'"$MAX_MODEL_LEN"',"rope_parameters":{"rope_type":"yarn","factor":'"$ROPE_FACTOR"',"original_max_position_embeddings":262144,"mrope_interleaved":true,"mrope_section":[11,11,10],"rope_theta":10000000,"partial_rotary_factor":0.25}}}'
fi

# Container env (model card DGX Spark block)
VLLM_PLE_TABLE_MEMORY="${VLLM_PLE_TABLE_MEMORY:-}"
[ -z "$VLLM_PLE_TABLE_MEMORY" ] && { [ "$PLE_OFFLOAD" = "0" ] && VLLM_PLE_TABLE_MEMORY=ram || VLLM_PLE_TABLE_MEMORY=disk; }
B12X_DISK_BACKEND="${B12X_DISK_BACKEND:-io_uring}"
CUTE_DSL_ARCH="${CUTE_DSL_ARCH:-sm_121a}"
SAFETENSORS_FAST_GPU="${SAFETENSORS_FAST_GPU:-1}"
VLLM_WORKER_MULTIPROC_METHOD="${VLLM_WORKER_MULTIPROC_METHOD:-spawn}"
VLLM_SSM_CONV_STATE_LAYOUT="${VLLM_SSM_CONV_STATE_LAYOUT:-DS}"
VLLM_USE_AOT_COMPILE="${VLLM_USE_AOT_COMPILE:-1}"
VLLM_USE_MEGA_AOT_ARTIFACT="${VLLM_USE_MEGA_AOT_ARTIFACT:-1}"
VLLM_USE_V2_MODEL_RUNNER="${VLLM_USE_V2_MODEL_RUNNER:-1}"
B12X_POLICY_MODE="${B12X_POLICY_MODE:-auto}"
VLLM_MXFP8_LM_HEAD="${VLLM_MXFP8_LM_HEAD:-1}"
VLLM_B12X_MOE_FP4_LAYER_MAX_INPUT_SCALE="${VLLM_B12X_MOE_FP4_LAYER_MAX_INPUT_SCALE:-w13}"
VLLM_B12X_MOE_FP4_FORCE_A16="${VLLM_B12X_MOE_FP4_FORCE_A16:-1}"
B12X_W4A16_A4_PREFILL="${B12X_W4A16_A4_PREFILL:-1}"
OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
EXTRA_ENV="${EXTRA_ENV:-}"

# Runtime
CONTAINER_NAME="${CONTAINER_NAME:-qwen38-flash-next}"
DETACH="${DETACH:-0}"
DRY_RUN="${DRY_RUN:-0}"

# --------------------------------------------------------------------------- #
# vLLM serve args
# --------------------------------------------------------------------------- #
MODEL_IN_CONTAINER=/model
ARGS=(
  serve "$MODEL_IN_CONTAINER"
  --host "$HOST" --port "$PORT" --served-model-name "$SERVED_NAME"
  --tensor-parallel-size "$TP" --pipeline-parallel-size "$PP"
  --dtype bfloat16 --quantization modelopt_mixed --load-format "$LOAD_FORMAT"
  --gpu-memory-utilization "$GPU_MEM_UTIL"
  --max-model-len "$MAX_MODEL_LEN" --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS"
  --kv-cache-dtype "$KV_CACHE_DTYPE" --block-size "$BLOCK_SIZE"
  --mamba-cache-mode "$MAMBA_CACHE_MODE"
  --linear-backend "$LINEAR_BACKEND" --moe-backend "$MOE_BACKEND"
  --gdn-decode-kernel "$GDN_DECODE_KERNEL"
  --disable-uvicorn-access-log
  --optimization-level "$OPTIMIZATION_LEVEL" --performance-mode "$PERFORMANCE_MODE"
)
[ -n "$HF_OVERRIDES" ] && ARGS+=(--hf-overrides "$HF_OVERRIDES")
[ "$ENABLE_PREFIX_CACHING" = "1" ] && ARGS+=(--enable-prefix-caching)
[ "$ENABLE_CHUNKED_PREFILL" = "1" ] && ARGS+=(--enable-chunked-prefill)
[ "$FUSE_ACT_QUANT" = "1" ] && ARGS+=(--compilation-config '{"pass_config":{"fuse_act_quant":true}}')
ARGS+=(--enable-auto-tool-choice)
[ -n "$TOOL_PARSER" ] && ARGS+=(--tool-call-parser "$TOOL_PARSER")
[ -n "$REASONING_PARSER" ] && ARGS+=(--reasoning-parser "$REASONING_PARSER")
case "$SPEC" in
  mtp) ARGS+=(--speculative-config '{"method":"mtp","num_speculative_tokens":'"$SPEC_TOKENS"'}') ;;
  none|off|disabled) ;;
  *) echo "ERROR: SPEC must be mtp or none (got: $SPEC)" >&2; exit 1 ;;
esac
# NOTE: EXTRA_ARGS is word-split on spaces; for values with spaces or JSON use
# the safe quoted passthrough instead:  ./starter-...sh -- --your-flag '{"a":1}'
# shellcheck disable=SC2206  # intentional word splitting of user-supplied flags
[ -n "$EXTRA_ARGS" ] && ARGS+=($EXTRA_ARGS)
[ "${1:-}" = "--" ] && { shift; ARGS+=("$@"); }

# --------------------------------------------------------------------------- #
# Container env
# --------------------------------------------------------------------------- #
ENV_ARGS=(
  -e HF_HUB_OFFLINE=1 -e TOKENIZERS_PARALLELISM=false
  -e VLLM_PLE_TABLE_MEMORY="$VLLM_PLE_TABLE_MEMORY"
  -e B12X_DISK_BACKEND="$B12X_DISK_BACKEND"
  -e CUTE_DSL_ARCH="$CUTE_DSL_ARCH"
  -e SAFETENSORS_FAST_GPU="$SAFETENSORS_FAST_GPU"
  -e VLLM_WORKER_MULTIPROC_METHOD="$VLLM_WORKER_MULTIPROC_METHOD"
  -e VLLM_SSM_CONV_STATE_LAYOUT="$VLLM_SSM_CONV_STATE_LAYOUT"
  -e VLLM_USE_AOT_COMPILE="$VLLM_USE_AOT_COMPILE"
  -e VLLM_USE_MEGA_AOT_ARTIFACT="$VLLM_USE_MEGA_AOT_ARTIFACT"
  -e VLLM_USE_V2_MODEL_RUNNER="$VLLM_USE_V2_MODEL_RUNNER"
  -e B12X_POLICY_MODE="$B12X_POLICY_MODE"
  -e VLLM_MXFP8_LM_HEAD="$VLLM_MXFP8_LM_HEAD"
  -e VLLM_B12X_MOE_FP4_LAYER_MAX_INPUT_SCALE="$VLLM_B12X_MOE_FP4_LAYER_MAX_INPUT_SCALE"
  -e VLLM_B12X_MOE_FP4_FORCE_A16="$VLLM_B12X_MOE_FP4_FORCE_A16"
  -e B12X_W4A16_A4_PREFILL="$B12X_W4A16_A4_PREFILL"
  -e OMP_NUM_THREADS="$OMP_NUM_THREADS"
  -e PYTORCH_CUDA_ALLOC_CONF="$PYTORCH_CUDA_ALLOC_CONF"
  -e VLLM_CACHE_ROOT=/root/.cache/vllm
  -e B12X_CACHE_DIR=/root/.cache/b12x
  -e TRITON_CACHE_DIR=/root/.triton/cache
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
  -e TRANSFORMERS_VERBOSITY=error
  -e TRANSFORMERS_NO_ADVISORY_WARNINGS=1
)
[ -n "$EXTRA_ENV" ] && for kv in $EXTRA_ENV; do ENV_ARGS+=(-e "$kv"); done

# --------------------------------------------------------------------------- #
# Launch
# --------------------------------------------------------------------------- #
CMD=(docker run)
[ "$DETACH" = "1" ] && CMD+=(-d --name "$CONTAINER_NAME") || CMD+=(--rm --name "$CONTAINER_NAME")
CMD+=(--network host --ipc host --gpus all --shm-size 16g
      --ulimit memlock=-1 --ulimit stack=67108864
      --security-opt seccomp=unconfined --security-opt apparmor=unconfined
      "${ENV_ARGS[@]}"
      -v "$MODEL_DIR":"$MODEL_IN_CONTAINER":ro
      -v "$HOME/.cache/huggingface":/root/.cache/huggingface
      -v "$HOME/.cache/vllm":/root/.cache/vllm
      -v "$HOME/.cache/flashinfer":/root/.cache/flashinfer
      -v "$HOME/.cache/triton":/root/.triton/cache
      -v "$HOME/.cache/b12x":/root/.cache/b12x
      --entrypoint vllm "$IMAGE" "${ARGS[@]}")

if [ "$DRY_RUN" = "1" ]; then
  printf '[dry-run]'; printf ' %q' "${CMD[@]}"; printf '\n'; exit 0
fi

echo "+ ${CMD[*]}"
[ "$DETACH" != "1" ] && exec "${CMD[@]}"

# Detached: wait for health, then smoke test
"${CMD[@]}"
API_HOST="$HOST"; { [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; } && API_HOST=127.0.0.1
case "$API_HOST" in *:*) API_HOST="[$API_HOST]";; esac
URL="http://$API_HOST:$PORT/v1"

echo -n "waiting for $URL"
start=$SECONDS
until curl -sf --max-time 5 "$URL/models" >/dev/null 2>&1; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" != "true" ]; then
    echo; echo "---- last log lines ----" >&2
    docker logs --tail 40 "$CONTAINER_NAME" 2>&1 | sed 's/^/  | /' >&2
    echo "ERROR: the server exited before it was ready." >&2
    exit 1
  fi
  [ $((SECONDS - start)) -ge 1200 ] && { echo; echo "ERROR: not ready after 1200s" >&2; exit 1; }
  printf '.'; sleep 3
done
echo " ready after $((SECONDS - start))s"

BODY='{"model":"'"$SERVED_NAME"'","max_tokens":32,"temperature":0,
  "chat_template_kwargs":{"enable_thinking":false},
  "messages":[{"role":"user","content":"Reply with OK."}]}'
if curl -s --max-time 300 "$URL/chat/completions" -H 'Content-Type: application/json' -d "$BODY" | \
   python3 -c 'import json,sys; r=json.load(sys.stdin); c=r["choices"][0]["message"].get("content") or ""; assert c.strip(); print("smoke OK:", repr(c.strip()[:40]), r["usage"]["completion_tokens"], "tokens")' 2>/dev/null; then
  :
else
  echo "WARNING: smoke test failed - server still running; check: docker logs $CONTAINER_NAME" >&2
fi
echo "API      $URL   (model: $SERVED_NAME)"
echo "Stop     docker rm -f $CONTAINER_NAME"
echo "Logs     docker logs -f $CONTAINER_NAME"
