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
    rebuild_tables: bool = False


MIGRATIONS = (
    Migration(1, tuple(part.strip() for part in SCHEMA.split(";") if part.strip())),
    Migration(2, (
        "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium' CHECK(priority IN ('high','medium','low'))",
        "ALTER TABLE users ADD COLUMN task_sort TEXT NOT NULL DEFAULT 'time' CHECK(task_sort IN ('time','priority'))",
    )),
    Migration(3, (
        'CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id INTEGER REFERENCES users(id))',
        "INSERT INTO categories VALUES ('study','🎓 Учёба',NULL),('work','💼 Работа',NULL),('personal','🏠 Личное',NULL),('shopping','🛒 Покупки',NULL),('other','📌 Другое',NULL)",
        "ALTER TABLE tasks ADD COLUMN category TEXT NOT NULL DEFAULT 'other'",
        "ALTER TABLE users ADD COLUMN task_category TEXT NOT NULL DEFAULT 'all'",
        'CREATE INDEX tasks_category ON tasks(user_id, category, status)',
    )),
    Migration(4, (
        'ALTER TABLE tasks ADD COLUMN advance_from INTEGER NOT NULL DEFAULT 0',
        'CREATE TABLE task_advances (task_id INTEGER NOT NULL REFERENCES tasks(id), minutes INTEGER NOT NULL CHECK(minutes IN (5,15,30,60,1440)), PRIMARY KEY(task_id,minutes))',
        '''CREATE TABLE deliveries_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id), generation INTEGER NOT NULL,
            scheduled_at INTEGER NOT NULL, due_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','completed','cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
            lease_until INTEGER, claim_token TEXT, message_id INTEGER, sent_at INTEGER,
            last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'main' CHECK(kind IN ('main','advance')),
            offset_minutes INTEGER NOT NULL DEFAULT 0,
            CHECK((kind='main' AND offset_minutes=0) OR (kind='advance' AND offset_minutes IN (5,15,30,60,1440))),
            UNIQUE(task_id,generation,scheduled_at,kind,offset_minutes)
        )''',
        '''INSERT INTO deliveries_v4(id,task_id,generation,scheduled_at,due_at,status,attempts,
            next_attempt_at,lease_until,claim_token,message_id,sent_at,last_error,created_at,updated_at)
            SELECT id,task_id,generation,scheduled_at,due_at,status,attempts,
            next_attempt_at,lease_until,claim_token,message_id,sent_at,last_error,created_at,updated_at FROM deliveries''',
        'DROP TABLE deliveries',
        'ALTER TABLE deliveries_v4 RENAME TO deliveries',
        'CREATE INDEX deliveries_due ON deliveries(status,next_attempt_at,lease_until)',
        'CREATE INDEX deliveries_task ON deliveries(task_id,generation,status,due_at)',
    )),
    Migration(5, (
        'ALTER TABLE users ADD COLUMN summary_enabled INTEGER NOT NULL DEFAULT 0 CHECK(summary_enabled IN (0,1))',
        "ALTER TABLE users ADD COLUMN summary_time TEXT NOT NULL DEFAULT '08:00'",
        'ALTER TABLE users ADD COLUMN summary_empty INTEGER NOT NULL DEFAULT 0 CHECK(summary_empty IN (0,1))',
        'ALTER TABLE users ADD COLUMN summary_from INTEGER NOT NULL DEFAULT 0',
        '''CREATE TABLE daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
            local_day TEXT NOT NULL, scheduled_at INTEGER NOT NULL, text TEXT NOT NULL, timezone TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','skipped')),
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
            lease_until INTEGER, claim_token TEXT, message_id INTEGER, sent_at INTEGER,
            UNIQUE(user_id,local_day)
        )''',
    )),
    Migration(6, (
        '''CREATE TABLE subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id),
            title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
            done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),
            deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
            revision INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        )''',
        'CREATE INDEX subtasks_parent ON subtasks(task_id,deleted,id)',
    )),
    Migration(7, (
        '''CREATE TABLE tasks_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200), remind_at INTEGER,
            repeat TEXT NOT NULL CHECK(repeat IN ('none','daily','weekdays','weekly')),
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','deleted')),
            timezone TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            priority TEXT NOT NULL DEFAULT 'medium' CHECK(priority IN ('high','medium','low')),
            category TEXT NOT NULL DEFAULT 'other', advance_from INTEGER NOT NULL DEFAULT 0,
            CHECK(remind_at IS NOT NULL OR repeat='none')
        )''',
        '''INSERT INTO tasks_v7(id,user_id,title,remind_at,repeat,status,timezone,generation,revision,
            created_at,updated_at,priority,category,advance_from)
            SELECT id,user_id,title,remind_at,repeat,status,timezone,generation,revision,
            created_at,updated_at,priority,category,advance_from FROM tasks''',
        'DROP TABLE tasks',
        'ALTER TABLE tasks_v7 RENAME TO tasks',
        'CREATE INDEX tasks_owner_time ON tasks(user_id,status,remind_at)',
        'CREATE INDEX tasks_due ON tasks(status,remind_at)',
        'CREATE INDEX tasks_category ON tasks(user_id,category,status)',
    ), rebuild_tables=True),
    Migration(8, (
        '''CREATE TABLE task_completions (
            task_id INTEGER NOT NULL REFERENCES tasks(id), user_id INTEGER NOT NULL REFERENCES users(id),
            generation INTEGER NOT NULL, occurrence_key TEXT NOT NULL, scheduled_at INTEGER,
            completed_at INTEGER NOT NULL, PRIMARY KEY(task_id,generation,occurrence_key)
        )''',
        '''INSERT OR IGNORE INTO task_completions SELECT d.task_id,t.user_id,d.generation,
            CAST(d.scheduled_at AS TEXT),d.scheduled_at,d.updated_at
            FROM deliveries d JOIN tasks t ON t.id=d.task_id WHERE d.kind='main' AND d.status='completed' ''',
        '''INSERT OR IGNORE INTO task_completions SELECT id,user_id,generation,
            COALESCE(CAST(remind_at AS TEXT),'inbox'),remind_at,updated_at
            FROM tasks WHERE repeat='none' AND status='completed' ''',
        'CREATE INDEX completions_owner_time ON task_completions(user_id,completed_at)',
    )),
    Migration(9, (
        '''CREATE TABLE habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),revision INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL
        )''',
        '''CREATE TABLE habit_checks (
            habit_id INTEGER NOT NULL REFERENCES habits(id),local_day TEXT NOT NULL,
            timezone TEXT NOT NULL,checked_at INTEGER NOT NULL,done INTEGER NOT NULL DEFAULT 1 CHECK(done IN (0,1)),
            PRIMARY KEY(habit_id,local_day)
        )''',
        'CREATE INDEX habits_owner ON habits(user_id,active,id)',
    )),
)


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
    # Rebuilding a referenced table requires disabling FK enforcement before
    # BEGIN. This startup connection is not yet available to application code;
    # foreign_key_check still validates all links before committing.
    rebuild = any(m.version > current and m.rebuild_tables for m in MIGRATIONS)
    if rebuild:
        await connection.execute('PRAGMA foreign_keys = OFF')
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
    finally:
        if rebuild:
            await connection.execute('PRAGMA foreign_keys = ON')
