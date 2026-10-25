"""SQLite, схема и короткие сериализованные транзакции.

Сетевых вызовов внутри транзакций нет. BEGIN IMMEDIATE защищает выдачу
напоминаний также при нескольких соединениях с одним файлом SQLite.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite


from bot.database.migrations import migrate


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
            await migrate(connection, self.path)
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
