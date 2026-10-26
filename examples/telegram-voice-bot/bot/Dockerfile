FROM python:3.12-slim
# ffmpeg converts Telegram's OGG/Opus voice messages to WAV for the STT server.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
USER nobody
CMD ["python", "-u", "bot.py"]
