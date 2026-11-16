# Starter: Qwen3.8-27B-NVFP4 with the DFlash2 drafter in the official vLLM Docker image (port 8101).
#
# Paste this whole file into + Add recipe and review the draft. It is qwen38-dflash.yaml in the other
# format: a plain `docker run` that the manager reads the fields out of - useful when you already have
# the command that works on your machine.
#
# Model and drafter sit in one folder (~/models here): the main model becomes /models/Qwen3.8-27B-NVFP4,
# the drafter /models/Qwen3.8-27B-DFlash2. Point -v at your own folder, or let the Weights tab find it
# under DGXM_MODEL_DIR and switch the recipe to `weights:` afterwards (Recipe file… exports it back).
#
# vllm/vllm-openai already runs `vllm serve` as its entrypoint, so the command starts at the model path.
# HF_TOKEN is passed by name from the manager's .env. Check --gpu-memory-utilization, --max-model-len
# and --max-num-seqs against your GPU.
docker run --gpus all \
  --ipc=host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --name qwen38-dflash-docker \
  -p 8101:8000 \
  -v ~/models:/models:ro \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -e HF_TOKEN \
  vllm/vllm-openai:v0.33.0 \
  /models/Qwen3.8-27B-NVFP4 \
    --served-model-name qwen3.8 \
    --gpu-memory-utilization 0.84 \
    --max-model-len 262144 \
    --max-num-seqs 4 \
    --max-num-batched-tokens 16384 \
    --kv-cache-dtype fp8 \
    --trust-remote-code \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    --reasoning-parser qwen3 \
    --async-scheduling \
    --no-enable-prefix-caching \
    --speculative-config '{"method":"dflash","model":"/models/Qwen3.8-27B-DFlash2","num_speculative_tokens":7}' \
    --host 0.0.0.0 \
    --port 8000
