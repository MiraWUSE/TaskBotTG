"""Environment configuration with startup-time validation."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from bot.utils.datetime_utils import validate_timezone


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: укажите целое число от {minimum} до {maximum}.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: допустимы значения от {minimum} до {maximum}.")
    return value


def _poll_seconds() -> float:
    try:
        value = float(os.getenv("REMINDER_POLL_SECONDS", "5").strip())
    except ValueError as exc:
        raise ValueError("REMINDER_POLL_SECONDS: укажите число от 0.1 до 300.") from exc
    if not math.isfinite(value) or not 0.1 <= value <= 300:
        raise ValueError("REMINDER_POLL_SECONDS: допустимы значения от 0.1 до 300.")
    return value


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Bot token is intentionally hidden from repr/logs."""

    bot_token: str = field(repr=False)
    bot_timezone: str = "Europe/Moscow"
    database_path: Path = Path("data/bot.sqlite3")
    reminder_poll_seconds: float = 5.0
    reminder_lease_seconds: int = 120
    telegram_timeout_seconds: int = 30
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        """Load .env without overriding process variables, then validate it."""
        load_dotenv(override=False)
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Не задан BOT_TOKEN. Создайте .env по образцу .env.example и укажите токен BotFather.")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Некорректный BOT_TOKEN. Скопируйте токен, выданный BotFather.")

        timezone = validate_timezone(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))
        path_text = os.getenv("DATABASE_PATH", "data/bot.sqlite3").strip()
        if not path_text or "\x00" in path_text or path_text == ":memory:":
            raise ValueError("DATABASE_PATH: укажите путь к файлу SQLite для постоянного хранения данных.")
        database_path = Path(path_text).expanduser()
        try:
            if database_path.exists() and database_path.is_dir():
                raise ValueError("DATABASE_PATH должен указывать на файл SQLite, а не на папку.")
        except OSError as exc:
            raise ValueError("DATABASE_PATH: некорректный путь к файлу SQLite.") from exc

        poll_seconds = _poll_seconds()
        lease_seconds = _integer("REMINDER_LEASE_SECONDS", 120, 1, 86400)
        timeout_seconds = _integer("TELEGRAM_TIMEOUT_SECONDS", 30, 1, 300)
        if lease_seconds <= timeout_seconds:
            raise ValueError("REMINDER_LEASE_SECONDS должен быть больше TELEGRAM_TIMEOUT_SECONDS.")
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError("LOG_LEVEL: используйте DEBUG, INFO, WARNING, ERROR или CRITICAL.")

        return cls(
            bot_token=token,
            bot_timezone=timezone,
            database_path=database_path,
            reminder_poll_seconds=poll_seconds,
            reminder_lease_seconds=lease_seconds,
            telegram_timeout_seconds=timeout_seconds,
            log_level=log_level,
        )
