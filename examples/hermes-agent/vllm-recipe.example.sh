# Starter: Hermes-ready vLLM model server (port 8101, served as "local")
#
# Paste this whole file into the manager's Recipes -> Import, review the draft, create.
# It is the MODEL recipe the Hermes gateway talks to (see ../docker-recipe.example.sh):
#   model.base_url: http://host.docker.internal:8101/v1   (docker gateway)
#                   http://127.0.0.1:8101/v1               (native gateway)
#   model.default:  local
#
# Weights: download once to the folder the volume mounts, e.g.
#   hf download Qwen/Qwen3-8B --local-dir ~/models/HERMES_MODEL
# Replace Qwen/Qwen3-8B with any tool-calling model you like. Every model you want to
# switch between gets its own copy of this script with the SAME port 8101 and
# --served-model-name local; then "Switch to" changes the model and the running gateway
# keeps working.
#
# After import the draft is named "local" (the served name); rename it in the editor.
# Tuned for a small GPU box - lower --gpu-memory-utilization / --max-model-len on bigger
# models or a busier machine.

# Reasoning models (Qwen3-Coder needs its parser as well): uncomment to add it.
# ARGS="$ARGS --reasoning-parser qwen3_coder"

docker run -d --name hermes-model \
   --gpus all \
   --ipc=host --ulimit memlock=-1 --ulimit stack=67108864 \
   -p 8101:8000 \
   -v ~/models/HERMES_MODEL:/model:ro \
   -v ~/.cache/huggingface:/root/.cache/huggingface \
   -e HF_TOKEN \
   nvcr.io/nvidia/vllm:25.11-py3 \
   serve /model --host 0.0.0.0 --port 8000 \
   --served-model-name local \
   --enable-auto-tool-choice \
   --tool-call-parser hermes \
   $ARGS
