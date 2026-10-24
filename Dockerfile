FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/app/data/bot.sqlite3

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --home-dir /app --shell /usr/sbin/nologin bot \
    && mkdir -p /app/data \
    && chown bot:bot /app/data

COPY --chown=bot:bot bot ./bot

USER bot
STOPSIGNAL SIGTERM
CMD ["python", "-m", "bot.main"]
