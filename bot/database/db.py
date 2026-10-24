"""SQLite, схема и короткие сериализованные транзакции.

Сетевых вызовов внутри транзакций нет. BEGIN IMMEDIATE защищает выдачу
напоминаний также при нескольких соединениях с одним файлом SQLite.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    timezone TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
    remind_at INTEGER NOT NULL,
    repeat TEXT NOT NULL CHECK(repeat IN ('none', 'daily', 'weekdays', 'weekly')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'completed', 'deleted')),
    timezone TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_owner_time ON tasks(user_id, status, remind_at);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status, remind_at);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    generation INTEGER NOT NULL,
    scheduled_at INTEGER NOT NULL,
    due_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'sending', 'sent', 'completed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    lease_until INTEGER,
    claim_token TEXT,
    message_id INTEGER,
    sent_at INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(task_id, generation, scheduled_at)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_attempt_at, lease_until);
CREATE INDEX IF NOT EXISTS deliveries_task ON deliveries(task_id, generation, status, due_at);
PRAGMA user_version = 1;
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._connection is not None:
            return
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser().resolve())
        connection = await aiosqlite.connect(self.path, isolation_level=None)
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA synchronous = FULL")
            async with connection.execute("PRAGMA user_version") as cursor:
                version = (await cursor.fetchone())[0]
            if version > 1:
                raise RuntimeError("Версия базы данных новее версии приложения.")
            await connection.executescript(SCHEMA)
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    def _get_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("База данных ещё не подключена.")
        return self._connection

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            yield self._get_connection()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            connection = self._get_connection()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
