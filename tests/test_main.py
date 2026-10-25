import logging
import subprocess
import sys

from bot.main import COMMANDS, SecretFormatter


def test_registered_commands():
    assert [command.command for command in COMMANDS] == ["start", "new", "tasks", "today", "calendar", "inbox", "stats", "habits", "settings", "help", "cancel"]


def test_log_formatter_redacts_token_even_in_traceback():
    token = "12345:super_secret"
    try:
        raise RuntimeError("https://api.telegram.org/bot" + token + "/getMe")
    except RuntimeError:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Ошибка %s", (token,), sys.exc_info())
    message = SecretFormatter(token).format(record)
    assert token not in message and "[BOT_TOKEN]" in message


def test_startup_without_token_is_clear_and_offline(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-m", "bot.main"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 2
    assert "BOT_TOKEN" in result.stderr and "Traceback" not in result.stderr


async def test_run_cancellation_stops_polling_and_worker_before_db_close(tmp_path, monkeypatch):
    import asyncio
    import pytest
    from bot import main as entry
    from bot.config import Settings
    from bot.database.db import Database
    from bot.lifecycle import GracefulDispatcher

    events = []
    polling_started = asyncio.Event()

    class OfflineDispatcher(GracefulDispatcher):
        async def _polling(self, *args, **kwargs):
            polling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("polling stopped")

    class RecordingDatabase(Database):
        async def close(self):
            events.append("db closed")
            await super().close()

    class Session:
        def __init__(self, **kwargs):
            pass

        async def close(self):
            events.append("session closed")

    class Bot:
        id = 123

        def __init__(self, token, *, session, **kwargs):
            self.session = session

        async def delete_webhook(self, **kwargs):
            assert kwargs["drop_pending_updates"] is False
            events.append("webhook deleted")

        async def set_my_commands(self, commands, **kwargs):
            assert len(commands) == 11
            events.append("commands registered")

    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, stop):
            await stop.wait()
            events.append("worker stopped")

    monkeypatch.setattr(entry, "GracefulDispatcher", OfflineDispatcher)
    monkeypatch.setattr(entry, "Database", RecordingDatabase)
    monkeypatch.setattr(entry, "AiohttpSession", Session)
    monkeypatch.setattr(entry, "Bot", Bot)
    monkeypatch.setattr(entry, "ReminderWorker", Worker)
    monkeypatch.setattr(entry, "configure_logging", lambda settings: None)
    settings = Settings(bot_token="123:offline", database_path=tmp_path / "lifecycle.sqlite3")
    running = asyncio.create_task(entry.run(settings))
    await asyncio.wait_for(polling_started.wait(), 3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 3)
    assert events.index("polling stopped") < events.index("db closed")
    assert events.index("worker stopped") < events.index("db closed") < events.index("session closed")
