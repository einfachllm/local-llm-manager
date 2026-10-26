# Telegram voice bot (example)

Talk to your own LLM from Telegram – by voice or text. Speech-to-text (Whisper) and the LLM run on your
machine, e.g. a DGX Spark; the bot only connects **out** to Telegram, so no port has to be open to the internet.

```
Telegram ──voice/text──► bot ──audio──► Whisper (this stack, port 8120)
                          │
                          └──text────► LLM recipe in Local LLM Manager (e.g. port 8101)
```

- Voice messages are transcribed, the transcript is shown (🎤 …), then the LLM answers.
- Text messages go straight to the LLM. The bot remembers the last 10 exchanges per chat (`/reset` clears them).
- Only the Telegram user IDs in `ALLOWED_USER_IDS` get answers.
- The LLM is not part of this stack on purpose: run it as a recipe in Local LLM Manager, so you can switch,
  schedule and update it. With `LLM_MODEL` empty the bot uses whatever model the port serves, so
  **Switch to** another recipe on the same port changes the bot's model without touching the bot.

## Setup

1. **Create the bot:** in Telegram, talk to `@BotFather` → `/newbot` → copy the token.
2. **Run an LLM** as a recipe in Local LLM Manager (any OpenAI-compatible server: vLLM, SGLang, llama.cpp…)
   and note its port.
3. **Configure and start** (on the machine with the GPU, e.g. the DGX Spark):
   ```bash
   cd examples/telegram-voice-bot
   cp .env.example .env
   nano .env                 # TELEGRAM_BOT_TOKEN, LLM_URL (port of your recipe); ALLOWED_USER_IDS later
   docker compose up -d --build
   ```
   The first start downloads Whisper (~1.6 GB) and can take a few minutes.
4. **Allow yourself:** send `/start` to your bot – it replies with your user ID. Put it into
   `ALLOWED_USER_IDS` in `.env` and run `docker compose up -d` again.
5. Send a voice message. 🎤

Logs: `docker compose logs -f bot` (and `whisper`).

## Notes

- **GPU memory:** vLLM reserves a fixed share of GPU memory. `WHISPER_GPU_SHARE=0.08` (~10 GB on a 128 GB
  DGX Spark) leaves the rest for your LLM recipe. Without it, vLLM would take ~90 % and the LLM could not start.
- **Reaching the LLM:** from the bot container, the machine itself is `host.docker.internal`. If Local LLM
  Manager runs with `DGXM_PUBLISH_IP=tailscale`, model ports listen only on the Tailscale IP – use that in
  `LLM_URL`.
- **Language:** Whisper detects it per message (German, Ukrainian, English, … all work). Set `STT_LANGUAGE`
  (e.g. `de`) if short messages are detected wrongly.
- **Privacy:** STT and LLM run locally, but voice messages pass through Telegram's servers (bot chats are not
  end-to-end encrypted). For sensitive content, use a local interface instead.
- **Whisper in Local LLM Manager instead of Compose:** you can also run Whisper as a normal recipe (vLLM
  framework, model `openai/whisper-large-v3-turbo`, argument `--gpu-memory-utilization 0.08`, health path
  `/health`) and point `STT_URL` at its port. Then start only the `bot` service.
- **Untested combinations:** the image tag must have an arm64 build for the DGX Spark. If an older vLLM
  refuses Whisper, add `--task transcription` to the `whisper` command, or use another OpenAI-compatible STT
  server and set `STT_URL` accordingly.

## Ideas to extend

- Voice replies with a local TTS server (Piper, Chatterbox, Kokoro) via `reply_voice`.
- Streaming answers by editing the reply message while tokens arrive.
- Per-user system prompts or a `/model` command that switches recipes through the Local LLM Manager API.
