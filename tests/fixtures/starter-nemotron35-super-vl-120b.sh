#!/usr/bin/env bash
#
# Starter: nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 on NVIDIA DGX Spark (GB10, SM121)
#
#   ./starter-nemotron35-super-vl-120b.sh                # foreground, logs to the terminal
#   DETACH=1 ./starter-nemotron35-super-vl-120b.sh       # background + wait for health + smoke test
#   DRY_RUN=1 ./starter-nemotron35-super-vl-120b.sh      # print the docker command only
#   SPEC=dflash ./starter-nemotron35-super-vl-120b.sh    # LiLiCorr drafter (recommended on Spark; needs download)
#   ./starter-nemotron35-super-vl-120b.sh -- --enforce-eager   # extra vLLM flags after --
#
# Any knob is an env var, e.g.:
#   IMAGE=vllm/vllm-openai:v0.32.0 MAX_MODEL_LEN=1048576 GPU_MEM_UTIL=0.8 PORT=8081 ...
#   MAX_NUM_SEQS=4 DETACH=1 ./starter-nemotron35-super-vl-120b.sh
# Stop:  docker rm -f nemotron-super-vl     Logs:  docker logs -f nemotron-super-vl
#
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-$HOME/models/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4}"
SERVED_NAME="${SERVED_NAME:-nemotron-3.5-super-vl}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8081}"
API_KEY="${API_KEY:-}"                               # empty => no --api-key

IMAGE="${IMAGE:-vllm/vllm-openai:nightly-3f1bcfd82bb2e93fcd364fc7a5491718453a77e2}"  # guide image:cu130-nightly is stale (vllm 0.19, no NemotronH_Omni_Reasoning_V3); use a recent nightly >= 0.32
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.75}"                 # forum recipe: 0.75
MAX_MODEL_LEN="${MAX_MODEL_LEN:-1048576}"            # forum recipe: 1M context (needs VLLM_ALLOW_LONG_MAX_MODEL_LEN=1)
MAX_NUM_SEQS="${MAX_NUM_SEQS:-1}"                    # forum recipe: 1
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"  # forum recipe: 8192
BLOCK_SIZE="${BLOCK_SIZE:-64}"                       # forum recipe: 64
PREFIX_MATCH_UNIT="${PREFIX_MATCH_UNIT:-64}"         # forum recipe: 64
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"              # guide: fp8
DTYPE="${DTYPE:-auto}"                               # guide: auto
QUANTIZATION="${QUANTIZATION:-fp4}"                  # guide: fp4
TP="${TP:-1}"
PP="${PP:-1}"
DP="${DP:-1}"
ASYNC_SCHEDULING="${ASYNC_SCHEDULING:-1}"            # guide: --async-scheduling
ENABLE_CHUNKED_PREFILL="${ENABLE_CHUNKED_PREFILL:-1}"  # guide: --enable-chunked-prefill
MOE_BACKEND="${MOE_BACKEND:-marlin}"                 # guide: Marlin for NVFP4 MoE (FlashInfer FP4 MoE is multi-GPU only)
MAMBA_SSM_CACHE_DTYPE="${MAMBA_SSM_CACHE_DTYPE:-float16}"  # forum recipe: float16
MAMBA_BACKEND="${MAMBA_BACKEND:-flashinfer}"         # forum recipe: --mamba-backend flashinfer
ENABLE_MAMBA_STOCHASTIC="${ENABLE_MAMBA_STOCHASTIC:-1}"  # forum recipe: --enable-mamba-cache-stochastic-rounding
MAMBA_PHILOX_ROUNDS="${MAMBA_PHILOX_ROUNDS:-5}"      # forum recipe: --mamba-cache-philox-rounds 5
ENABLE_MAMBA_SHARED_PREFIX="${ENABLE_MAMBA_SHARED_PREFIX:-1}"  # forum recipe: --enable-mamba-shared-prefix-checkpoint
NVFP4_GEMM_BACKEND="${NVFP4_GEMM_BACKEND:-marlin}"   # guide: Marlin NVFP4 GEMM
USE_FLASHINFER_MOE_FP4="${USE_FLASHINFER_MOE_FP4:-0}"          # guide: 0 (disable FlashInfer FP4 MoE)
FLASHINFER_ALLREDUCE_BACKEND="${FLASHINFER_ALLREDUCE_BACKEND:-trtllm}"  # guide: trtllm (single-GPU allreduce)
ALLOW_LONG_MAX_MODEL_LEN="${ALLOW_LONG_MAX_MODEL_LEN:-1}"      # guide: required for 1M context
REASONING_PARSER="${REASONING_PARSER-nemotron_v3}"  # forum recipe: nemotron_v3 (built into vLLM, no plugin needed)
TOOL_PARSER="${TOOL_PARSER-qwen3_coder}"             # guide: qwen3_coder
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"          # guide: --trust-remote-code

VIDEO_PRUNING_RATE="${VIDEO_PRUNING_RATE:-0.5}"
LIMIT_MM="${LIMIT_MM:-{\"video\":1,\"image\":4}}"  # forum recipe
MEDIA_IO_KWARGS="${MEDIA_IO_KWARGS:-{\"video\":{\"fps\":2,\"num_frames\":256}}}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

SPEC="${SPEC:-dflash}"                              # forum recipe: LiLiCorr dflash (recommended on Spark)
DRAFT_MODEL="${DRAFT_MODEL:-/models/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr}"  # local path (forum recipe: LiLiCorr drafter)
SPEC_TOKENS="${SPEC_TOKENS:-7}"                      # forum recipe: 7 (LiLiCorr)
SPEC_MOE_BACKEND="${SPEC_MOE_BACKEND:-marlin}"       # MTP draft-path MoE backend (triton not supported for NvFP4 MoE in current nightlies)

CONTAINER_NAME="${CONTAINER_NAME:-nemotron-super-vl}"
DETACH="${DETACH:-0}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-1800}"
DRY_RUN="${DRY_RUN:-0}"
MEDIA_PATH="${MEDIA_PATH:-}"                         # extra read-only host path, e.g. ~/media

MODEL_IN_CONTAINER=/model
ARGS=(
  serve "$MODEL_IN_CONTAINER"
  --host "$HOST"
  --port "$PORT"
  --dtype "$DTYPE"
  --kv-cache-dtype "$KV_CACHE_DTYPE"
  --tensor-parallel-size "$TP"
  --pipeline-parallel-size "$PP"
  --data-parallel-size "$DP"
  --gpu-memory-utilization "$GPU_MEM_UTIL"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS"
  --block-size "$BLOCK_SIZE"
  --prefix-match-unit "$PREFIX_MATCH_UNIT"
  --mamba-backend "$MAMBA_BACKEND"
  --mamba-ssm-cache-dtype "$MAMBA_SSM_CACHE_DTYPE"
  --allowed-local-media-path "$MODEL_IN_CONTAINER"
  --limit-mm-per-prompt "$LIMIT_MM"
  --video-pruning-rate "$VIDEO_PRUNING_RATE"
  --media-io-kwargs "$MEDIA_IO_KWARGS"
  --enable-auto-tool-choice
  --enable-prompt-tokens-details
)
[ -n "$QUANTIZATION" ] && ARGS+=(--quantization "$QUANTIZATION")
[ -n "$MOE_BACKEND" ] && ARGS+=(--moe-backend "$MOE_BACKEND")
[ "$TRUST_REMOTE_CODE" = "1" ] && ARGS+=(--trust-remote-code)
[ "$ASYNC_SCHEDULING" = "1" ] && ARGS+=(--async-scheduling)
[ "$ENABLE_CHUNKED_PREFILL" = "1" ] && ARGS+=(--enable-chunked-prefill)
[ "$ENABLE_MAMBA_STOCHASTIC" = "1" ] && ARGS+=(--enable-mamba-cache-stochastic-rounding)
[ "$ENABLE_MAMBA_SHARED_PREFIX" = "1" ] && ARGS+=(--enable-mamba-shared-prefix-checkpoint)
ARGS+=(--mamba-cache-philox-rounds "$MAMBA_PHILOX_ROUNDS")
[ -n "$SERVED_NAME" ] && ARGS+=(--served-model-name "$SERVED_NAME")
[ -n "$API_KEY" ] && ARGS+=(--api-key "$API_KEY")
[ -n "$TOOL_PARSER" ] && ARGS+=(--tool-call-parser "$TOOL_PARSER")
if [ -n "$REASONING_PARSER" ]; then
  ARGS+=(--reasoning-parser "$REASONING_PARSER")
fi
case "$SPEC" in
  mtp)
    ARGS+=(--speculative-config '{"method":"mtp","num_speculative_tokens":'"$SPEC_TOKENS"',"moe_backend":"'"$SPEC_MOE_BACKEND"'"}')
    ;;
  dflash)
    DRAFT_JSON='{"method":"dflash","model":"'"$DRAFT_MODEL"'","num_speculative_tokens":'"$SPEC_TOKENS"',"rejection_sample_method":"standard","attention_backend":"FLASHINFER","draft_load_config":{"load_format":"auto"}}'
    ARGS+=(--speculative-config "$DRAFT_JSON")
    ;;
  none|off|disabled) ;;
  *) echo "ERROR: SPEC must be mtp, dflash or none (got: $SPEC)" >&2; exit 1 ;;
esac
if [ -n "$EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206
  ARGS+=($EXTRA_ARGS)
fi
if [ "${1:-}" = "--" ]; then shift; ARGS+=("$@"); fi

echo "== Nemotron-3.5-Super-VL-120B-A12B-NVFP4 vLLM starter ====================="
echo "model:      $MODEL_DIR"
echo "image:      $IMAGE"
echo "=============================================================================="

[ -f "$MODEL_DIR/config.json" ] || { echo "ERROR: no config.json in $MODEL_DIR" >&2; exit 1; }
grep -q "NemotronH_Omni_Reasoning_V3" "$MODEL_DIR/config.json" || echo "WARNING: $MODEL_DIR/config.json is not a Nemotron-3.5 Super VL checkpoint." >&2
[ -f "$MODEL_DIR/model.safetensors.index.json" ] || echo "WARNING: no model.safetensors.index.json - incomplete download?" >&2
WEIGHTS_GIB=$(du -sb "$MODEL_DIR" 2>/dev/null | awk '{printf "%.1f", $1 / 1073741824}')
AVAIL_GIB=$(awk '/MemAvailable/ {printf "%.0f", $2 / 1048576}' /proc/meminfo)

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "ERROR: nvidia-smi not found - this starter expects an NVIDIA DGX Spark (GB10)." >&2
  exit 1
fi
ARCH="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ' || true)"
echo "gpu:        $(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1), SM ${ARCH:--}, ${AVAIL_GIB} GiB MemAvailable, weights ${WEIGHTS_GIB} GiB"
if [ -n "$ARCH" ] && [ "${ARCH%%.*}" -lt 12 ] 2>/dev/null; then
  echo "WARNING: this checkpoint targets Blackwell SM120/SM121; detected SM $ARCH." >&2
fi

BUSY="$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null || true)"
if [ -n "$BUSY" ]; then
  echo "WARNING: other processes hold GPU/unified memory - vLLM may OOM:" >&2
  printf '%s\n' "$BUSY" | sed 's/^/           /' >&2
  echo "         e.g. docker rm -f vllm-qwen" >&2
fi
NEED_GIB=$(awk -v w="$WEIGHTS_GIB" 'BEGIN {printf "%.0f", w * 1.2 + 6}')

if [ "$SPEC" = "dflash" ]; then
  DRAFT_PATH="${DRAFT_MODEL#/models/}"
  DRAFT_PATH="${DRAFT_PATH#$HOME/models/}"
  if [ ! -d "$HOME/models/$DRAFT_PATH" ]; then
    echo "WARNING: SPEC=dflash drafter not found at ~/models/$DRAFT_PATH" >&2
    echo "         Download once: hf download nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr --local-dir ~/models/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr" >&2
  fi
fi

case "$IMAGE" in
  *:v0.31.0|*:v0.31.0-*)
    if [ "$SPEC" != "none" ] && [ "$SPEC" != "off" ] && [ "$SPEC" != "disabled" ]; then
      echo "WARNING: vllm/vllm-openai:v0.31.0 skips the 'language_model.mtp.*' weights, so SPEC=$SPEC
         will not load the MTP/draft head. Use the nightly (default) or set SPEC=none." >&2
    fi ;;
esac

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found." >&2; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY_RUN: docker pull $IMAGE would run now"
  else
    echo "pulling $IMAGE ..."
    docker pull "$IMAGE"
  fi
fi

if [ "$AVAIL_GIB" -lt "$NEED_GIB" ]; then
  if [ "$DRY_RUN" = "1" ]; then echo "WARNING: ${AVAIL_GIB} GiB available, ~${NEED_GIB} GiB likely needed" >&2
  else
    echo "ERROR: only ${AVAIL_GIB} GiB MemAvailable; ~${NEED_GIB} GiB is needed for ${WEIGHTS_GIB} GiB of weights." >&2
    echo "       Stop the other vLLM/GPU workloads first (e.g. docker rm -f vllm-qwen) or set FORCE=1." >&2
    [ "${FORCE:-0}" = "1" ] || exit 1
  fi
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  if [ "$DRY_RUN" = "1" ]; then echo "DRY_RUN: container $CONTAINER_NAME exists and would be removed"
  else
    echo "removing previous container $CONTAINER_NAME"
    docker rm -f "$CONTAINER_NAME" >/dev/null
  fi
fi
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
  if [ "$DRY_RUN" = "1" ]; then echo "WARNING: port $PORT is already in use" >&2
  else
    echo "ERROR: port $PORT is already in use: $(ss -ltnp "sport = :$PORT" 2>/dev/null | tail -n +2)" >&2
    exit 1
  fi
fi

ENV_ARGS=(
  -e HF_HUB_OFFLINE=1 -e TOKENIZERS_PARALLELISM=false
  -e "VLLM_NVFP4_GEMM_BACKEND=$NVFP4_GEMM_BACKEND"
  -e "VLLM_USE_FLASHINFER_MOE_FP4=$USE_FLASHINFER_MOE_FP4"
  -e "VLLM_FLASHINFER_ALLREDUCE_BACKEND=$FLASHINFER_ALLREDUCE_BACKEND"
)
[ "$ALLOW_LONG_MAX_MODEL_LEN" = "1" ] && ENV_ARGS+=(-e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1)

CMD=(docker run)
if [ "$DETACH" = "1" ]; then CMD+=(-d --name "$CONTAINER_NAME"); else CMD+=(--rm --name "$CONTAINER_NAME"); fi
CMD+=(--network host --ipc host --gpus all
      --ulimit memlock=-1 --ulimit stack=67108864
      "${ENV_ARGS[@]}"
      -v "$MODEL_DIR":"$MODEL_IN_CONTAINER":ro
      -v "$HOME/models":/models:ro)
if [ -n "$MEDIA_PATH" ]; then CMD+=(-v "$MEDIA_PATH":/media:ro); fi
if [ -n "${HF_TOKEN:-}" ]; then CMD+=(-e "HF_TOKEN=$HF_TOKEN"); fi
CMD+=(--entrypoint vllm "$IMAGE" "${ARGS[@]}")

echo
if [ "$DRY_RUN" = "1" ]; then
  printf '[dry-run]'; printf ' %q' "${CMD[@]}"; printf '\n'
  exit 0
fi
echo "+ ${CMD[*]}"
if [ "$DETACH" != "1" ]; then exec "${CMD[@]}"; fi
