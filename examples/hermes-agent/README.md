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
  keeps working with the new model after **Switch to** (restart the gateway, see step 3).
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

Add a recipe, **Native process**, on the same host:

| Field | Value |
|---|---|
| Name | `hermes-gateway` |
| Command | `/home/you/.local/bin/hermes gateway run` (the path from `which hermes`) |
| Port | a free placeholder port, e.g. `8199` (Hermes does not listen on it) |
| Health path | *(empty - the gateway has no HTTP endpoint)* |

Start it and watch the log in the dashboard; then message your bot on Telegram, by text or voice.

- Do **not** also run `hermes gateway install` / `hermes gateway start`: two gateways polling the same bot
  token fight over the messages. The manager is the service manager here.
- **Switch to** stops *every* other recipe on that host - the gateway too. After switching the LLM, press
  **Start** on `hermes-gateway` again (it loads in seconds). In a schedule, add a "start hermes-gateway" a
  few minutes after the "switch to". Never use **Switch to** on the gateway recipe itself: it would stop
  the LLM.
- A schedule "stop all" in the evening and "start" of both recipes in the morning gives you a bot that
  sleeps at night.

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
