from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from bot.config import Settings


TOKEN = "123456789:" + "A" * 35


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in (
        "BOT_TOKEN", "BOT_TIMEZONE", "DATABASE_PATH", "REMINDER_POLL_SECONDS",
        "REMINDER_LEASE_SECONDS", "TELEGRAM_TIMEOUT_SECONDS", "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("bot.config.load_dotenv", lambda **kwargs: False)


def test_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    settings = Settings.from_env()
    assert settings.bot_token == TOKEN
    assert settings.bot_timezone == "Europe/Moscow"
    assert settings.database_path == Path("data/bot.sqlite3")
    assert settings.reminder_poll_seconds == 5
    assert settings.reminder_lease_seconds == 120
    assert settings.telegram_timeout_seconds == 30
    assert settings.log_level == "INFO"
    assert TOKEN not in repr(settings)
    with pytest.raises(FrozenInstanceError):
        settings.log_level = "DEBUG"


def test_custom_settings(monkeypatch, tmp_path):
    values = {
        "BOT_TOKEN": TOKEN,
        "BOT_TIMEZONE": " Asia/Kathmandu ",
        "DATABASE_PATH": str(tmp_path / "bot.db"),
        "REMINDER_POLL_SECONDS": "0.5",
        "REMINDER_LEASE_SECONDS": "90",
        "TELEGRAM_TIMEOUT_SECONDS": "20",
        "LOG_LEVEL": "debug",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.bot_timezone == "Asia/Kathmandu"
    assert settings.database_path == tmp_path / "bot.db"
    assert settings.reminder_poll_seconds == 0.5
    assert settings.reminder_lease_seconds == 90
    assert settings.telegram_timeout_seconds == 20
    assert settings.log_level == "DEBUG"


def test_dotenv_is_loaded_without_overriding_process_environment(monkeypatch):
    called = []
    monkeypatch.setattr("bot.config.load_dotenv", lambda **kwargs: called.append(kwargs))
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    Settings.from_env()
    assert called == [{"override": False}]


@pytest.mark.parametrize("token", [None, "", " ", "abc:secret", "1234:", "1234:has space", "1234:abc/def", "NO_TOKEN"])
def test_invalid_or_missing_token_never_appears_in_error(monkeypatch, token):
    if token is not None:
        monkeypatch.setenv("BOT_TOKEN", token)
    with pytest.raises(ValueError, match="BOT_TOKEN") as error:
        Settings.from_env()
    if token and token.strip():
        assert token not in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BOT_TIMEZONE", "Wrong/Zone"),
        ("BOT_TIMEZONE", ""),
        ("DATABASE_PATH", ""),
        ("DATABASE_PATH", ":memory:"),
        ("REMINDER_POLL_SECONDS", "nan"),
        ("REMINDER_POLL_SECONDS", "inf"),
        ("REMINDER_POLL_SECONDS", "-1"),
        ("REMINDER_POLL_SECONDS", "0"),
        ("REMINDER_POLL_SECONDS", "0.01"),
        ("REMINDER_POLL_SECONDS", "301"),
        ("REMINDER_POLL_SECONDS", "five"),
        ("REMINDER_LEASE_SECONDS", "0"),
        ("REMINDER_LEASE_SECONDS", "86401"),
        ("REMINDER_LEASE_SECONDS", "30.5"),
        ("TELEGRAM_TIMEOUT_SECONDS", "0"),
        ("TELEGRAM_TIMEOUT_SECONDS", "301"),
        ("TELEGRAM_TIMEOUT_SECONDS", "bad"),
        ("LOG_LEVEL", "VERBOSE"),
    ],
)
def test_invalid_configuration(monkeypatch, name, value):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("lease", [29, 30])
def test_lease_must_exceed_telegram_timeout(monkeypatch, lease):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("REMINDER_LEASE_SECONDS", str(lease))
    with pytest.raises(ValueError, match="больше TELEGRAM_TIMEOUT_SECONDS"):
        Settings.from_env()


def test_database_path_cannot_be_a_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path))
    with pytest.raises(ValueError, match="папку"):
        Settings.from_env()
