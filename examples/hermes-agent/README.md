# Hermes Agent on your own LLM

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research, MIT) is a personal AI agent
with a Telegram gateway, tools, memory and built-in voice: it transcribes your voice messages and can answer
with a voice bubble. This guide runs it entirely on your own machine:

```
Telegram ⇄ Hermes gateway (recipe, native process)
              ├─ speech-to-text: faster-whisper, local      (built into Hermes)
              ├─ text-to-speech: Piper, local               (built into Hermes)
              └─ LLM: http://127.0.0.1:8101/v1               (recipe, vLLM/SGLang/llama.cpp/MLX)
```

Compared to the [Telegram voice bot](../telegram-voice-bot/) you need no separate Whisper server or bot
code - Hermes brings speech in and out itself - but you get a full agent that can run commands, so read
[Security](#security) first.

Both the LLM and the Hermes gateway are recipes in this manager: you see their status and logs in the
dashboard, a schedule can stop them at night, and **Switch to** changes the agent's model.

Needs: a Linux box or Mac with this manager, a model that supports tool calling, a Telegram bot token.

> This is a configuration guide, not a tested package: Hermes changes fast. If a key below is renamed,
> the [Hermes docs](https://hermes-agent.nousresearch.com/docs) win.

## 1. The LLM recipe

An agent needs tool calling. Create (or adjust) a recipe on the machine that will run Hermes:

- Port `8101` (any free port; it only has to match `base_url` below).
- Add to the arguments, for vLLM:
  ```
  --served-model-name local --enable-auto-tool-choice --tool-call-parser hermes
  ```
  Use the parser that fits the model (`hermes` for Qwen2.5/Qwen3 and Hermes models — recent vLLM also has
  `qwen3_xml`, and thinking models need a matching `--reasoning-parser` — `qwen3_coder` for Qwen3-Coder,
  `llama3_json` for Llama 3, …; see the vLLM tool-calling docs). For SGLang the flags are
  `--served-model-name local --tool-call-parser <parser>`; llama.cpp needs `--jinja`.
- Give every model you want to switch between the **same port** and `--served-model-name local`: Hermes then
  keeps working with the new model after **Switch to** (see step 3).
- Pick a model with a long context (Hermes sends a large system prompt and tool list; 32k+ tokens).

Start it and check: `curl http://127.0.0.1:8101/v1/models` lists `local`.

## 2. Install and configure Hermes

On the same machine, as the user the manager runs as:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
which hermes          # note the absolute path for step 3
```

`~/.hermes/config.yaml` (merge into what the installer created):

```yaml
model:
  provider: custom
  base_url: http://127.0.0.1:8101/v1
  default: local
  key_env: MY_LLM_API_KEY   # name of the .env variable holding the key, if the endpoint needs one

stt:
  provider: local          # faster-whisper on this machine, nothing leaves it
  local:
    model: turbo           # tiny | base | small | medium | large-v3 | turbo

tts:
  provider: piper          # local; the default "edge" uses Microsoft's cloud

approvals:
  mode: manual             # ask before risky commands; never "off"
```

`~/.hermes/.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC…            # from @BotFather
TELEGRAM_ALLOWED_USERS=123456789          # your numeric Telegram user id (@userinfobot)
# HERMES_LOCAL_STT_LANGUAGE=de           # optional: fix the language instead of auto-detect
```

Install `ffmpeg` (`sudo apt install ffmpeg` / `brew install ffmpeg`): Hermes needs it to convert voice
messages and to send voice bubbles. Try it once in the terminal with `hermes` before going on.

## 3. The Hermes gateway as a recipe

Add a recipe on the same host with **+ New agent** (or pick *This recipe runs: An agent* in the editor).
That puts it in the dashboard's **Agents** section, and **Switch to** on a model leaves it running:

| Field | Value |
|---|---|
| Runtime | Native process |
| Name | `hermes-gateway` |
| Command | `/home/you/.local/bin/hermes gateway run` (the path from `which hermes`) |
| Port | a free placeholder port, e.g. `8199` (Hermes does not listen on it) |
| Health path | *(empty - the gateway has no HTTP endpoint)* |
| Model | e.g. `local (Qwen3-30B on :8101)` - only shown on the card |
| Environment | your secret **names**, e.g. `TELEGRAM_BOT_TOKEN` (see the Docker variant below) |

Start it and watch the log in the dashboard; then message your bot on Telegram, by text or voice.

- Do **not** also run `hermes gateway install` / `hermes gateway start`: two gateways polling the same bot
  token fight over the messages. The manager is the service manager here.
- **Switch to** on a model stops the other *models* on that host, not agents: the gateway keeps running
  and talks to the new model on the same port. If replies fail right after a switch (the model is still
  loading), wait until it is *Ready*, or press **Restart** on the gateway. Agents have no **Switch to**.
- The agent card shows each secret name with ✓ (set in the manager's environment) or ✗ *not set*, and warns
  when a second copy of the gateway command runs outside the manager (see Gotchas).
- A schedule "stop all" in the evening and "start" of both recipes in the morning gives you a bot that
  sleeps at night.

### Or run it in Docker

Same idea as a **Docker** recipe (framework *Custom (Docker)*), using the official image - one folder of
state plus the recipe definition, and no Hermes install on the host to maintain:

| Field | Value |
|---|---|
| This recipe runs | An agent |
| Image | `nousresearch/hermes-agent` |
| Command | `gateway run` - and only that: never override the entrypoint, the image needs its s6 `/init` |
| Volumes | `~/.hermes:/opt/data` (same config, keys and sessions as the native install) |
| Port | a free placeholder, e.g. `8198` (the gateway does not listen on it) |
| Health path | *(empty)* |
| GPUs | *(empty - the default `all` would make `docker run` fail on machines without NVIDIA)* |
| Environment | `HERMES_UID=1000`, `HERMES_GID=1000`, plus your secret **names** |

For the secrets (Telegram token, model API key): keep the **values** in the manager's own `.env` file on
the host and list only the bare **names** (`TELEGRAM_BOT_TOKEN`, one per line) in the recipe. The manager
passes them through from its environment, so no secret ever lands in the recipe, the database or the
dashboard.

No typing needed: [`docker-recipe.example.sh`](docker-recipe.example.sh) is an importable version of
exactly this command - paste it into **Recipes → Import**, clear the pre-filled *Health path*, adjust
`HERMES_UID/GID`, create.

## Gotchas worth knowing

- `hermes config set` **saves nothing** while `config.yaml` has a parse error (a duplicated key, bad
  indentation): Hermes quietly runs the last good copy. Check what is live with `hermes config get model`.
- Without `model.key_env`, Hermes falls back to `OPENAI_API_KEY` / `OPENROUTER_API_KEY` from the
  environment - and a recipe inherits the **manager's** environment. A stray cloud key there then
  surfaces as a puzzling "invalid API key" from your local endpoint. Set `model.key_env` explicitly.
- Hermes' own update/restart logic can spawn a detached standalone gateway next to your recipe; two
  gateways polling one bot token means lost messages. If replies go missing, look for a second
  `gateway run` process, kill it, restart the recipe.

## Voice quality and speed

- Speech-to-text: `turbo` (large-v3-turbo) is close to the best Whisper quality and fast; it runs on the CPU
  unless faster-whisper finds a CUDA GPU. On a CPU-only box, `small` answers much faster.
- Text-to-speech: Piper is fast and fully local but sounds more synthetic than cloud voices. For a nicer
  local voice, run an OpenAI-compatible TTS server (e.g. Kokoro-FastAPI) - as another recipe - and point
  Hermes' TTS at it (see the Hermes voice docs).

## Security

Hermes is an agent: it can run shell commands and edit files as the user it runs as - which, as a recipe,
is the manager's user, who can also start any container.

- Keep `TELEGRAM_ALLOWED_USERS` to yourself. Anyone on that list effectively has a shell on the machine.
- Keep `approvals.mode` at `manual` (or `smart`), never `off`, and add `approvals.deny` patterns for
  commands it must never run.
- Better: use Hermes' **docker terminal backend**, so commands run in a container instead of on the host.
  Inside it Hermes skips its own dangerous-command checks (the container is the boundary), so do not
  mount your home directory or the Docker socket into it.
- The gateway only connects outward (Telegram long polling); it opens no port. Your voice messages and the
  answers do pass through Telegram's servers; the transcription and the model stay on your machine.

See the [Hermes security docs](https://hermes-agent.nousresearch.com/docs) for the full list of options.
