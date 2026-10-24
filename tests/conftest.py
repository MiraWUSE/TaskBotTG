from datetime import UTC, datetime, timedelta

import pytest

from bot.database.db import Database
from bot.services.task_service import TaskService


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def service(tmp_path, clock):
    async with Database(tmp_path / "tasks.sqlite3") as database:
        service = TaskService(database, clock=clock)
        await service.ensure_user(1, 1)
        await service.ensure_user(2, 2)
        yield service
