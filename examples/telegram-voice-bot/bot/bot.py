"""Telegram voice bot: voice or text in, local STT (Whisper) + local LLM, text answer back.

Everything runs on your own machine. The bot only connects *out* to Telegram (long polling), so no port
has to be reachable from the internet. Configuration comes from environment variables (see .env.example).
"""

import asyncio
import logging
import os
from collections import defaultdict, deque

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
STT_URL = os.environ.get("STT_URL", "http://127.0.0.1:8120/v1/audio/transcriptions")
STT_MODEL = os.environ.get("STT_MODEL", "openai/whisper-large-v3-turbo")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "")  # e.g. "de"; empty = detect
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8101/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "")  # empty = ask the server (GET /v1/models)
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant. Answer briefly and in the user's language.")
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "10"))  # remembered question/answer pairs per chat
TELEGRAM_LIMIT = 4096

log = logging.getLogger("voice-bot")
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=2 * HISTORY_TURNS))


def allowed(update: Update) -> bool:
    user = update.effective_user
    if user and user.id in ALLOWED:
        return True
    log.warning("Ignoring message from user %s (not in ALLOWED_USER_IDS)", user.id if user else "?")
    return False


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram messages are limited to 4096 characters: split at paragraph or line breaks where possible."""
    parts = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit))
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + [text] if text else parts


async def to_wav(audio: bytes) -> bytes:
    """Telegram voice messages are OGG/Opus; convert to 16 kHz mono WAV, which every STT server accepts."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(audio)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace').strip()}")
    return out


async def transcribe(http: httpx.AsyncClient, audio: bytes) -> str:
    data = {"model": STT_MODEL}
    if STT_LANGUAGE:
        data["language"] = STT_LANGUAGE
    r = await http.post(STT_URL, files={"file": ("voice.wav", await to_wav(audio), "audio/wav")}, data=data)
    r.raise_for_status()
    return r.json()["text"].strip()


async def llm_model(http: httpx.AsyncClient) -> str:
    """The configured model, or the first one the server lists - so switching models needs no bot change."""
    if LLM_MODEL:
        return LLM_MODEL
    r = await http.get(LLM_URL.rsplit("/chat/completions", 1)[0] + "/models", headers=llm_headers())
    r.raise_for_status()
    return r.json()["data"][0]["id"]


def llm_headers() -> dict:
    return {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}


async def answer(http: httpx.AsyncClient, chat_id: int, text: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[chat_id], {"role": "user", "content": text}]
    r = await http.post(LLM_URL, headers=llm_headers(), json={"model": await llm_model(http), "messages": messages})
    r.raise_for_status()
    reply = (r.json()["choices"][0]["message"].get("content") or "").strip() or "(empty answer)"
    history[chat_id].extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
    return reply


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE, voice: bool) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as http:
            if voice:
                media = update.message.voice or update.message.audio
                audio = bytes(await (await media.get_file()).download_as_bytearray())
                text = await transcribe(http, audio)
                if not text:
                    await update.message.reply_text("🤷 I could not understand the recording.")
                    return
                await update.message.reply_text(f"🎤 {text}")
                await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            else:
                text = update.message.text
            reply = await answer(http, chat_id, text)
    except httpx.ConnectError as e:
        log.warning("Model server not reachable: %s", e)
        await update.message.reply_text("⚠️ The model server is not reachable - is the recipe running?")
        return
    except Exception as e:  # report every failure to the user instead of staying silent
        log.exception("Request failed")
        await update.message.reply_text(f"⚠️ {type(e).__name__}: {e}"[:500])
        return
    for part in split_message(reply):
        await update.message.reply_text(part)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, ctx, voice=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, ctx, voice=False)


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and user.id in ALLOWED:
        await update.message.reply_text("Hi! Send me a voice message or text. /reset forgets the conversation.")
    else:
        # Tells you your ID once, so you can add it to ALLOWED_USER_IDS; nothing else is answered.
        await update.message.reply_text(f"This bot is private. Your Telegram user ID is {user.id if user else '?'}.")


async def on_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        history.pop(update.effective_chat.id, None)
        await update.message.reply_text("🧹 Conversation forgotten.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would print the bot token
    if not ALLOWED:
        raise SystemExit("Set ALLOWED_USER_IDS (comma-separated Telegram user IDs); send /start to the bot to see yours")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("reset", on_reset))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot running (STT %s, LLM %s, %d allowed user(s))", STT_URL, LLM_URL, len(ALLOWED))
    app.run_polling()  # outbound only: works behind NAT and inside a Tailscale-only setup


if __name__ == "__main__":
    main()
