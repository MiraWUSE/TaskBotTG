"""Ordered SQLite migrations; never edit an already published migration."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bot.backup import backup_database


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
"""


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


MIGRATIONS = (Migration(1, tuple(part.strip() for part in SCHEMA.split(";") if part.strip())),)


async def schema_version(connection) -> int:
    async with connection.execute("PRAGMA user_version") as cursor:
        return (await cursor.fetchone())[0]


async def migrate(connection, path: str) -> None:
    versions = [migration.version for migration in MIGRATIONS]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError("Нарушен порядок миграций SQLite.")
    current = await schema_version(connection)
    latest = versions[-1]
    if current > latest:
        raise RuntimeError("Версия базы данных новее версии приложения.")
    if current == latest:
        return
    async with connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") as cursor:
        existing = await cursor.fetchall()
    if current == 0 and existing:
        raise RuntimeError("Неизвестная схема без версии; требуется ручная проверка базы.")
    if current and path != ":memory:":
        source = Path(path)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = source.parent / "backups" / f"pre-migration-v{current}-{stamp}-{uuid4().hex[:8]}.sqlite3"
        # Backup uses a separate connection before the write transaction.
        # Failure aborts startup; no schema changes have happened yet.
        await asyncio.to_thread(backup_database, source, output)
    await connection.execute("BEGIN IMMEDIATE")
    try:
        # Another connection may have completed the migration while we waited.
        current = await schema_version(connection)
        if current > latest:
            raise RuntimeError("Версия базы данных новее версии приложения.")
        for migration in MIGRATIONS:
            if migration.version > current:
                for statement in migration.statements:
                    await connection.execute(statement)
                await connection.execute(f"PRAGMA user_version = {migration.version}")
        async with connection.execute("PRAGMA foreign_key_check") as cursor:
            if await cursor.fetchone() is not None:
                raise RuntimeError("Миграция нарушила связи SQLite.")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise
