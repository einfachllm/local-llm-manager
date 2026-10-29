# Starter: nvidia/Qwen3.8-Flash-Next-FP4-Local on NVIDIA DGX Spark (GB10, SM121)
#
# Paste this whole file into the manager's Recipes -> Import, review the draft, create.
#
# Weights: download once to the folder the first volume mounts, e.g.
#   hf download nvidia/Qwen3.8-Flash-Next-FP4-Local --local-dir ~/models/Qwen3.8-Flash-Next-FP4-Local
# The rest of the volumes are caches - optional, they only speed up later starts.
#
# The b12x flags and the 524k context are tuned for the Spark's 128 GB unified memory.
# On other GPUs: lower --gpu-memory-utilization / --max-model-len and drop the b12x flags.
#
# Endpoint afterwards: http://<host>:8081/v1, model name "qwen38-flash-next-fp4"
# (--served-model-name). For agent switching keep this port across your models.

docker run -d --name qwen38-flash-next \
  --gpus all \
  --network host --ipc host --shm-size 16g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --entrypoint vllm \
  -e HF_HUB_OFFLINE=1 \
  -e TOKENIZERS_PARALLELISM=false \
  -e VLLM_PLE_TABLE_MEMORY=disk \
  -e B12X_DISK_BACKEND=io_uring \
  -e CUTE_DSL_ARCH=sm_121a \
  -e SAFETENSORS_FAST_GPU=1 \
  -e VLLM_WORKER_MULTIPROC_METHOD=spawn \
  -e VLLM_SSM_CONV_STATE_LAYOUT=DS \
  -e VLLM_USE_AOT_COMPILE=1 \
  -e VLLM_USE_MEGA_AOT_ARTIFACT=1 \
  -e VLLM_USE_V2_MODEL_RUNNER=1 \
  -e B12X_POLICY_MODE=auto \
  -e VLLM_MXFP8_LM_HEAD=1 \
  -e VLLM_B12X_MOE_FP4_LAYER_MAX_INPUT_SCALE=w13 \
  -e VLLM_B12X_MOE_FP4_FORCE_A16=1 \
  -e B12X_W4A16_A4_PREFILL=1 \
  -e OMP_NUM_THREADS=2 \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  -e VLLM_CACHE_ROOT=/root/.cache/vllm \
  -e B12X_CACHE_DIR=/root/.cache/b12x \
  -e TRITON_CACHE_DIR=/root/.triton/cache \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -e TRANSFORMERS_VERBOSITY=error \
  -e TRANSFORMERS_NO_ADVISORY_WARNINGS=1 \
  -v ~/models/Qwen3.8-Flash-Next-FP4-Local:/model:ro \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v ~/.cache/vllm:/root/.cache/vllm \
  -v ~/.cache/flashinfer:/root/.cache/flashinfer \
  -v ~/.cache/triton:/root/.triton/cache \
  -v ~/.cache/b12x:/root/.cache/b12x \
  eugr/spark-vllm-b12x:latest \
  serve /model --host 0.0.0.0 --port 8081 \
  --served-model-name qwen38-flash-next-fp4 \
  --tensor-parallel-size 1 \
  --pipeline-parallel-size 1 \
  --dtype bfloat16 \
  --quantization modelopt_mixed \
  --load-format b12x \
  --gpu-memory-utilization 0.89 \
  --max-model-len 524288 \
  --max-num-seqs 7 \
  --max-num-batched-tokens 16384 \
  --kv-cache-dtype fp8 \
  --block-size 16 \
  --mamba-cache-mode align \
  --linear-backend b12x \
  --moe-backend b12x \
  --gdn-decode-kernel b12x \
  --disable-uvicorn-access-log \
  --optimization-level 3 \
  --performance-mode throughput \
  --hf-overrides '{"max_position_embeddings":524288,"text_config":{"max_position_embeddings":524288,"rope_parameters":{"rope_type":"yarn","factor":2,"original_max_position_embeddings":262144,"mrope_interleaved":true,"mrope_section":[11,11,10],"rope_theta":10000000,"partial_rotary_factor":0.25}}}' \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --compilation-config '{"pass_config":{"fuse_act_quant":true}}' \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --speculative-config '{"method":"mtp","num_speculative_tokens":4}'
