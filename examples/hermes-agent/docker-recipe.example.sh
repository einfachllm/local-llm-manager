# Importable recipe example: Hermes Agent gateway as a Docker recipe.
#
# Paste this whole file into the manager's + Add recipe box and review the draft.
# Then clear the Health path field (the gateway opens no HTTP port) and create the recipe.
#
# --add-host makes host.docker.internal resolve to the host, so the gateway's config can reach
# the LLM on http://host.docker.internal:8101/v1 from inside the container.
#
# Secrets are NOT in this file: the bare names below are passed through from the
# manager's own environment, so put the values (TELEGRAM_BOT_TOKEN, ...) in the
# manager's .env on the host - see examples/hermes-agent/README.md, step 3.
#
# Set HERMES_UID/HERMES_GID to your own `id -u` / `id -g` so files the container
# creates under ~/.hermes stay owned by your user.
#
# Never override the entrypoint: the image needs its s6 /init (see README.md).

docker run -d --name hermes-agent-docker \
  -p 8198:8198 \
  --add-host=host.docker.internal:host-gateway \
  -v ~/.hermes:/opt/data \
  -e TELEGRAM_BOT_TOKEN \
  -e TELEGRAM_ALLOWED_USERS \
  -e EINFACHLLM_API_KEY \
  -e HERMES_LOCAL_STT_LANGUAGE=de \
  -e HERMES_UID=1000 \
  -e HERMES_GID=1000 \
  nousresearch/hermes-agent gateway run
