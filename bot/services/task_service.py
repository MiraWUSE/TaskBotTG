"""Проверка владельца, жизненный цикл задач и журнал доставок."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from weakref import WeakValueDictionary

import aiosqlite

from bot.database.db import Database
from bot.models import Delivery, Task, User
from bot.task_options import ADVANCES, PRIORITIES
from bot.utils.datetime_utils import local_day_bounds, next_occurrence, utc_now, validate_timezone

UTC = timezone.utc
REPEATS = {"none", "daily", "weekdays", "weekly"}
LIVE_DELIVERIES = "('pending', 'sending', 'sent')"


class ValidationError(ValueError):
    pass


class TaskNotFound(ValueError):
    def __init__(self) -> None:
        super().__init__("Задача или напоминание уже недоступны. Откройте список заново.")


async def _one(connection, query, parameters=()):
    async with connection.execute(query, parameters) as cursor:
        return await cursor.fetchone()


async def _all(connection, query, parameters=()):
    async with connection.execute(query, parameters) as cursor:
        return await cursor.fetchall()


def _datetime(value: int) -> datetime:
    # Windows CRT fromtimestamp ограничивает диапазон даже для aware UTC.
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=value)


def _user(row: aiosqlite.Row) -> User:
    return User(row["id"], row["chat_id"], row["timezone"], bool(row["blocked"]), row['task_sort'], row['task_category'],
                bool(row['summary_enabled']), row['summary_time'], bool(row['summary_empty']))


def _task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"], user_id=row["user_id"], title=row["title"],
        remind_at=_datetime(row["display_at"] if "display_at" in row.keys() else row["remind_at"]),
        repeat=row["repeat"], status=row["status"], timezone=row["timezone"],
        created_at=_datetime(row["created_at"]), updated_at=_datetime(row["updated_at"]),
        generation=row["generation"], revision=row["revision"],
        priority=row['priority'],
        category=row['category'], category_label=row['category_label'],
        advance_offsets=tuple(sorted(int(x) for x in (row['advance_offsets'] or '').split(',') if x)),
    )


def _delivery(row: aiosqlite.Row) -> Delivery:
    return Delivery(
        id=row["id"], task_id=row["task_id"], user_id=row["user_id"], title=row["title"],
        repeat=row["repeat"], timezone=row["timezone"], scheduled_at=_datetime(row["scheduled_at"]),
        due_at=_datetime(row["due_at"]), status=row["status"], attempts=row["attempts"],
        claim_token=row["claim_token"], message_id=row["message_id"], chat_id=row["chat_id"],
        priority=row['priority'],
        kind=row['kind'], offset_minutes=row['offset_minutes'], user_timezone=row['user_timezone'],
    )


# Пока пользователь не закрыл уже отправленное напоминание, оно остаётся
# актуальным и отображается в списке; будущий экземпляр серии хранится отдельно.
TASK_VIEW = f"""
WITH task_view AS (
    SELECT t.*, (SELECT group_concat(minutes) FROM task_advances a WHERE a.task_id=t.id) AS advance_offsets,
    (SELECT name FROM categories c WHERE c.id=t.category) AS category_label,
    CASE WHEN t.repeat='none' THEN COALESCE((
        SELECT MIN(d.due_at) FROM deliveries d
        WHERE d.task_id = t.id AND d.generation = t.generation
          AND d.kind='main' AND d.status IN {LIVE_DELIVERIES}
    ), t.remind_at) ELSE MIN(t.remind_at, COALESCE((
        SELECT MIN(d.due_at) FROM deliveries d
        WHERE d.task_id = t.id AND d.generation = t.generation
          AND d.kind='main' AND d.status IN {LIVE_DELIVERIES}
    ), t.remind_at)) END AS display_at FROM tasks t
)
"""
TASK_TODAY_VIEW = f"""
WITH task_candidates AS (
    SELECT t.*, (SELECT group_concat(minutes) FROM task_advances a WHERE a.task_id=t.id) AS advance_offsets,
    (SELECT name FROM categories c WHERE c.id=t.category) AS category_label, (
        SELECT MIN(d.due_at) FROM deliveries d
        WHERE d.task_id=t.id AND d.generation=t.generation
          AND d.kind='main' AND d.status IN {LIVE_DELIVERIES} AND d.due_at>=? AND d.due_at<?
    ) AS occurrence_at
    FROM tasks t
), task_view AS (
    SELECT *, CASE WHEN remind_at>=? AND remind_at<?
        THEN MIN(remind_at, COALESCE(occurrence_at, remind_at))
        ELSE occurrence_at END AS display_at FROM task_candidates
)
"""
DELIVERY_SELECT = """
SELECT d.*, t.user_id, t.title, t.repeat, t.timezone, t.priority, u.chat_id, u.timezone AS user_timezone
FROM deliveries d JOIN tasks t ON t.id = d.task_id JOIN users u ON u.id = t.user_id
"""


class TaskService:
    def __init__(self, database: Database, default_timezone: str = "Europe/Moscow",
                 clock: Callable[[], datetime] = utc_now):
        self.database = database
        self.default_timezone = validate_timezone(default_timezone)
        self._clock = clock
        self._task_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    def now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def task_lock(self, task_id: int) -> asyncio.Lock:
        """Один lock для отправки и изменений задачи внутри процесса."""
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[task_id] = lock
        return lock

    def _timestamp(self) -> int:
        return int(self.now().timestamp())

    def _future(self, value: datetime) -> int:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("Укажите дату и время с часовым поясом.")
        stamp = int(value.timestamp())
        if stamp <= self._timestamp():
            raise ValidationError("Нельзя создать напоминание в прошлом. Выберите будущее время.")
        return stamp

    @staticmethod
    def _title(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("Отправьте название текстом.")
        value = " ".join(value.split())
        if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
            raise ValidationError("Название должно содержать от 1 до 200 символов.")
        return value

    async def ensure_user(self, user_id: int, chat_id: int) -> User:
        if user_id <= 0 or chat_id != user_id:
            raise ValidationError("Бот работает только в личном чате.")
        now = self._timestamp()
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO users(id, chat_id, timezone, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE
                   SET chat_id=excluded.chat_id, blocked=0, updated_at=excluded.updated_at""",
                (user_id, chat_id, self.default_timezone, now, now),
            )
            return _user(await _one(connection, "SELECT * FROM users WHERE id=?", (user_id,)))

    async def get_user(self, user_id: int) -> User:
        async with self.database.read() as connection:
            row = await _one(connection, "SELECT * FROM users WHERE id=?", (user_id,))
        if row is None:
            raise ValidationError("Сначала отправьте /start.")
        return _user(row)

    async def set_timezone(self, user_id: int, timezone: str) -> User:
        try:
            timezone = validate_timezone(timezone)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        async with self.task_lock(-user_id), self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE users SET timezone=?, updated_at=? WHERE id=?",
                (timezone, self._timestamp(), user_id),
            )
            if not cursor.rowcount:
                raise ValidationError("Сначала отправьте /start.")
            await connection.execute("UPDATE daily_summaries SET status='skipped',claim_token=NULL,lease_until=NULL WHERE user_id=? AND status IN ('pending','sending')", (user_id,))
            return _user(await _one(connection, "SELECT * FROM users WHERE id=?", (user_id,)))

    async def create_task(self, user_id: int, title: str, remind_at: datetime,
                          repeat: str = "none", *, priority: str = 'medium', category: str = 'other') -> Task:
        title = self._title(title)
        stamp = self._future(remind_at)
        if repeat not in REPEATS:
            raise ValidationError("Выберите допустимый вариант повторения.")
        if priority not in PRIORITIES:
            raise ValidationError('Выберите допустимый приоритет.')
        user = await self.get_user(user_id)
        if repeat == "weekdays":
            from zoneinfo import ZoneInfo
            if remind_at.astimezone(ZoneInfo(user.timezone)).weekday() >= 5:
                raise ValidationError("Для повтора по будням выберите дату с понедельника по пятницу.")
        now = self._timestamp()
        async with self.database.transaction() as connection:
            await self._category(connection, user_id, category)
            cursor = await connection.execute(
                """INSERT INTO tasks(user_id,title,remind_at,repeat,timezone,created_at,updated_at,priority,category,advance_from)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", (user_id, title, stamp, repeat, user.timezone, now, now, priority, category, now),
            )
            return _task(await self._owned_task(connection, user_id, cursor.lastrowid))

    @staticmethod
    async def _owned_task(connection, user_id: int, task_id: int):
        row = await _one(connection, "SELECT t.*, (SELECT group_concat(minutes) FROM task_advances a WHERE a.task_id=t.id) AS advance_offsets, (SELECT name FROM categories c WHERE c.id=t.category) AS category_label FROM tasks t WHERE id=? AND user_id=? AND status='active'",
                         (task_id, user_id))
        if row is None:
            raise TaskNotFound()
        return row

    async def get_task(self, user_id: int, task_id: int) -> Task:
        async with self.database.read() as connection:
            row = await _one(connection, TASK_VIEW +
                             "SELECT * FROM task_view WHERE id=? AND user_id=? AND status='active'",
                             (task_id, user_id))
        if row is None:
            raise TaskNotFound()
        return _task(row)

    async def list_tasks(self, user_id: int, *, today: bool = False, page: int = 0,
                         page_size: int = 6, sort_by: str | None = None,
                         category: str | None = None) -> tuple[list[Task], int]:
        if page < 0 or not 1 <= page_size <= 20:
            raise ValidationError("Неверная страница списка.")
        user = await self.get_user(user_id)
        sort_by = user.task_sort if sort_by is None else sort_by
        if sort_by not in {'time', 'priority'}:
            raise ValidationError('Неизвестная сортировка.')
        order = "display_at, id" if sort_by == 'time' else "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, display_at, id"
        where = "user_id=? AND status='active'"
        values: list = [user_id]
        view = TASK_VIEW
        if today:
            start, end = local_day_bounds(user.timezone, self.now())
            start_stamp, end_stamp = int(start.timestamp()), int(end.timestamp())
            view = TASK_TODAY_VIEW
            values = [start_stamp, end_stamp, start_stamp, end_stamp, user_id]
            where += " AND display_at IS NOT NULL"
        async with self.database.read() as connection:
            category = user.task_category if category is None else category
            if category != 'all':
                await self._category(connection, user_id, category)
                where += ' AND category=?'
                values.append(category)
            total = (await _one(connection, view + f"SELECT COUNT(*) FROM task_view WHERE {where}", values))[0]
            rows = await _all(connection, view + f"SELECT * FROM task_view WHERE {where} "
                              f"ORDER BY {order} LIMIT ? OFFSET ?", (*values, page_size, page * page_size))
        return [_task(row) for row in rows], total

    async def set_task_sort(self, user_id: int, sort_by: str) -> None:
        if sort_by not in {'time', 'priority'}:
            raise ValidationError('Неизвестная сортировка.')
        await self.get_user(user_id)
        async with self.database.transaction() as connection:
            await connection.execute('UPDATE users SET task_sort=?,updated_at=? WHERE id=?',
                                     (sort_by, self._timestamp(), user_id))

    async def set_priority(self, user_id: int, task_id: int, priority: str) -> Task:
        if priority not in PRIORITIES:
            raise ValidationError('Выберите допустимый приоритет.')
        async with self.task_lock(task_id), self.database.transaction() as connection:
            await self._owned_task(connection, user_id, task_id)
            await connection.execute('UPDATE tasks SET priority=?,revision=revision+1,updated_at=? WHERE id=? AND user_id=?',
                                     (priority, self._timestamp(), task_id, user_id))
        return await self.get_task(user_id, task_id)

    @staticmethod
    async def _category(connection, user_id: int, category: str):
        row = await _one(connection, 'SELECT * FROM categories WHERE id=? AND (owner_id IS NULL OR owner_id=?)',
                         (category, user_id))
        if row is None:
            raise ValidationError('Категория недоступна.')
        return row

    async def list_categories(self, user_id: int) -> list[tuple[str, str]]:
        async with self.database.read() as connection:
            rows = await _all(connection, 'SELECT id,name FROM categories WHERE owner_id IS NULL OR owner_id=? ORDER BY rowid', (user_id,))
        return [(row['id'], row['name']) for row in rows]

    async def set_category_filter(self, user_id: int, category: str) -> None:
        await self.get_user(user_id)
        async with self.database.transaction() as connection:
            if category != 'all':
                await self._category(connection, user_id, category)
            await connection.execute('UPDATE users SET task_category=?,updated_at=? WHERE id=?',
                                     (category, self._timestamp(), user_id))

    async def set_category(self, user_id: int, task_id: int, category: str) -> Task:
        async with self.task_lock(task_id), self.database.transaction() as connection:
            await self._owned_task(connection, user_id, task_id)
            await self._category(connection, user_id, category)
            await connection.execute('UPDATE tasks SET category=?,revision=revision+1,updated_at=? WHERE id=? AND user_id=?',
                                     (category, self._timestamp(), task_id, user_id))
        return await self.get_task(user_id, task_id)

    async def rename_task(self, user_id: int, task_id: int, title: str) -> Task:
        title = self._title(title)
        async with self.task_lock(task_id), self.database.transaction() as connection:
            await self._owned_task(connection, user_id, task_id)
            await connection.execute("UPDATE tasks SET title=?,revision=revision+1,updated_at=? WHERE id=? AND user_id=?",
                                     (title, self._timestamp(), task_id, user_id))
        return await self.get_task(user_id, task_id)

    @staticmethod
    async def _cancel_deliveries(connection, task_id: int, now: int) -> None:
        await connection.execute(
            f"UPDATE deliveries SET status='cancelled', claim_token=NULL,lease_until=NULL,updated_at=? "
            f"WHERE task_id=? AND status IN {LIVE_DELIVERIES}", (now, task_id),
        )

    async def reschedule_task(self, user_id: int, task_id: int, remind_at: datetime) -> Task:
        stamp = self._future(remind_at)
        user = await self.get_user(user_id)
        async with self.task_lock(task_id), self.database.transaction() as connection:
            task = await self._owned_task(connection, user_id, task_id)
            if task["repeat"] == "weekdays":
                from zoneinfo import ZoneInfo
                if remind_at.astimezone(ZoneInfo(user.timezone)).weekday() >= 5:
                    raise ValidationError("Для повтора по будням выберите рабочий день.")
            now = self._timestamp()
            await self._cancel_deliveries(connection, task_id, now)
            await connection.execute(
                "UPDATE tasks SET remind_at=?,timezone=?,generation=generation+1,revision=revision+1,updated_at=?,advance_from=? WHERE id=? AND user_id=?",
                (stamp, user.timezone, now, now, task_id, user_id),
            )
        return await self.get_task(user_id, task_id)

    async def delete_task(self, user_id: int, task_id: int) -> None:
        async with self.task_lock(task_id), self.database.transaction() as connection:
            await self._owned_task(connection, user_id, task_id)
            now = self._timestamp()
            await self._cancel_deliveries(connection, task_id, now)
            await connection.execute("UPDATE tasks SET status='deleted',revision=revision+1,updated_at=? WHERE id=? AND user_id=?",
                                     (now, task_id, user_id))

    async def complete_task(self, user_id: int, task_id: int) -> Task:
        async with self.task_lock(task_id), self.database.transaction() as connection:
            row = await self._owned_task(connection, user_id, task_id)
            now = self._timestamp()
            if row["repeat"] == "none":
                await self._cancel_deliveries(connection, task_id, now)
                await connection.execute("UPDATE tasks SET status='completed',revision=revision+1,updated_at=? WHERE id=? AND user_id=?",
                                         (now, task_id, user_id))
            else:
                occurrence = await _one(connection,
                    f"SELECT id,due_at,scheduled_at FROM deliveries WHERE task_id=? AND generation=? AND kind='main' AND status IN {LIVE_DELIVERIES} "
                    "ORDER BY due_at,id LIMIT 1", (task_id, row["generation"]))
                if occurrence and occurrence["due_at"] <= row["remind_at"]:
                    await self._cancel_advances(connection, task_id, now, occurrence['scheduled_at'])
                    await connection.execute(
                        "UPDATE deliveries SET status='completed',claim_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                        (now, occurrence["id"]),
                    )
                else:
                    await self._cancel_advances(connection, task_id, now, row['remind_at'])
                    next_time = next_occurrence(_datetime(row["remind_at"]), row["repeat"], row["timezone"],
                                                after=max(self.now(), _datetime(row["remind_at"])))
                    await connection.execute("UPDATE tasks SET remind_at=?,updated_at=? WHERE id=? AND user_id=?",
                                             (int(next_time.timestamp()), now, task_id, user_id))
                await connection.execute("UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=? AND user_id=?",
                                         (now, task_id, user_id))
            return _task(await _one(connection, TASK_VIEW + "SELECT * FROM task_view WHERE id=? AND user_id=?", (task_id, user_id)))

    @staticmethod
    async def _owned_delivery(connection, user_id: int, delivery_id: int):
        row = await _one(connection, DELIVERY_SELECT +
            " WHERE d.id=? AND t.user_id=? AND t.status='active' AND d.generation=t.generation AND d.kind='main' AND d.status='sent'",
            (delivery_id, user_id))
        if row is None:
            raise TaskNotFound()
        return row

    async def get_delivery(self, user_id: int, delivery_id: int) -> Delivery:
        async with self.database.read() as connection:
            return _delivery(await self._owned_delivery(connection, user_id, delivery_id))

    async def _finish_delivery(self, user_id: int, delivery_id: int, deleted: bool) -> None:
        delivery = await self.get_delivery(user_id, delivery_id)
        async with self.task_lock(delivery.task_id), self.database.transaction() as connection:
            row = await self._owned_delivery(connection, user_id, delivery_id)
            now = self._timestamp()
            await self._cancel_advances(connection, row['task_id'], now, row['scheduled_at'])
            await connection.execute("UPDATE deliveries SET status=?,updated_at=? WHERE id=?",
                                     ("cancelled" if deleted else "completed", now, delivery_id))
            if row["repeat"] == "none":
                await connection.execute("UPDATE tasks SET status=?,revision=revision+1,updated_at=? WHERE id=? AND user_id=?",
                    ("deleted" if deleted else "completed", now, row["task_id"], user_id))
            else:
                await connection.execute("UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=? AND user_id=?",
                                         (now, row["task_id"], user_id))

    async def complete_delivery(self, user_id: int, delivery_id: int) -> None:
        await self._finish_delivery(user_id, delivery_id, deleted=False)

    async def delete_delivery(self, user_id: int, delivery_id: int) -> None:
        await self._finish_delivery(user_id, delivery_id, deleted=True)

    async def snooze_delivery(self, user_id: int, delivery_id: int, remind_at: datetime) -> None:
        stamp = self._future(remind_at)
        delivery = await self.get_delivery(user_id, delivery_id)
        async with self.task_lock(delivery.task_id), self.database.transaction() as connection:
            row = await self._owned_delivery(connection, user_id, delivery_id)
            now = self._timestamp()
            await self._cancel_advances(connection, row['task_id'], now, row['scheduled_at'])
            await connection.execute(
                """UPDATE deliveries SET status='pending',due_at=?,next_attempt_at=?,attempts=0,
                   claim_token=NULL,lease_until=NULL,message_id=NULL,sent_at=NULL,last_error=NULL,
                   scheduled_at=CASE WHEN ?='none' THEN ? ELSE scheduled_at END, updated_at=? WHERE id=?""",
                (stamp, stamp, row["repeat"], stamp, now, delivery_id),
            )
            if row["repeat"] == "none":
                await connection.execute("UPDATE tasks SET remind_at=?,revision=revision+1,updated_at=?,advance_from=? WHERE id=? AND user_id=?",
                                         (stamp, now, now, row["task_id"], user_id))
            else:
                await connection.execute("UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=? AND user_id=?",
                                         (now, row["task_id"], user_id))

    @staticmethod
    async def _cancel_advances(connection, task_id: int, now: int, scheduled_at: int | None = None) -> None:
        query = "UPDATE deliveries SET status='cancelled',claim_token=NULL,lease_until=NULL,updated_at=? WHERE task_id=? AND kind='advance' AND status IN ('pending','sending')"
        values = [now, task_id]
        if scheduled_at is not None:
            query += ' AND scheduled_at=?'
            values.append(scheduled_at)
        await connection.execute(query, values)

    async def set_advances(self, user_id: int, task_id: int, offsets) -> Task:
        if not isinstance(offsets, (list, tuple, set)) or any(type(x) is not int or x not in ADVANCES for x in offsets):
            raise ValidationError('Выберите допустимые интервалы предварительного уведомления.')
        offsets = sorted(set(offsets))
        async with self.task_lock(task_id), self.database.transaction() as connection:
            await self._owned_task(connection, user_id, task_id)
            now = self._timestamp()
            await self._cancel_advances(connection, task_id, now)
            await connection.execute('DELETE FROM task_advances WHERE task_id=?', (task_id,))
            await connection.executemany('INSERT INTO task_advances(task_id,minutes) VALUES (?,?)',
                                         [(task_id, offset) for offset in offsets])
            await connection.execute('UPDATE tasks SET advance_from=?,revision=revision+1,updated_at=? WHERE id=? AND user_id=?',
                                     (now, now, task_id, user_id))
        return await self.get_task(user_id, task_id)

    async def _materialize_advances(self, connection, now: int) -> None:
        # Expired previews are never sent after their main deadline, including retries.
        await connection.execute("UPDATE deliveries SET status='cancelled',claim_token=NULL,lease_until=NULL,updated_at=? WHERE kind='advance' AND scheduled_at<=? AND status IN ('pending','sending')", (now, now))
        rows = await _all(connection, '''SELECT t.*, a.minutes FROM tasks t
            JOIN users u ON u.id=t.user_id JOIN task_advances a ON a.task_id=t.id
            WHERE t.status='active' AND u.blocked=0 AND t.remind_at>?''', (now,))
        for row in rows:
            targets = [row['remind_at']]
            # One following occurrence covers the largest (24h) offset even
            # across a 23-hour DST day. Future rows are durable, not an in-memory timer.
            if row['repeat'] != 'none':
                try:
                    following = next_occurrence(_datetime(row['remind_at']), row['repeat'], row['timezone'], after=_datetime(row['remind_at']))
                    targets.append(int(following.timestamp()))
                except ValueError:
                    pass
            for target in targets:
                due = target - row['minutes'] * 60
                if due <= row['advance_from']:
                    continue
                await connection.execute('''INSERT INTO deliveries(task_id,generation,scheduled_at,due_at,
                    next_attempt_at,created_at,updated_at,kind,offset_minutes) VALUES (?,?,?,?,?,?,?,'advance',?)
                    ON CONFLICT(task_id,generation,scheduled_at,kind,offset_minutes) DO UPDATE
                    SET status='pending',due_at=excluded.due_at,next_attempt_at=excluded.next_attempt_at,
                        attempts=0,last_error=NULL,updated_at=excluded.updated_at
                    WHERE deliveries.status='cancelled' AND deliveries.sent_at IS NULL''',
                    (row['id'], row['generation'], target, due, due, now, now, row['minutes']))

    async def materialize_due(self, limit: int = 100) -> int:
        """Сохранить наступившие экземпляры, догнать серии одним уведомлением.

        Для повторов сохраняем один пропущенный экземпляр, а расписание сразу
        продвигаем до будущего времени. Сбой отправки не задерживает всю серию.
        """
        now = self._timestamp()
        async with self.database.transaction() as connection:
            rows = await _all(connection, """
                SELECT t.* FROM tasks t JOIN users u ON u.id=t.user_id
                WHERE t.status='active' AND u.blocked=0 AND t.remind_at<=?
                  AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.task_id=t.id
                      AND d.generation=t.generation AND d.kind='main' AND d.scheduled_at=t.remind_at)
                ORDER BY t.remind_at,t.id LIMIT ?
                """, (now, limit))
            for row in rows:
                await connection.execute(
                    """INSERT INTO deliveries(task_id,generation,scheduled_at,due_at,next_attempt_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (row["id"], row["generation"], row["remind_at"], row["remind_at"], now, now, now),
                )
                if row["repeat"] != "none":
                    try:
                        next_time = next_occurrence(_datetime(row["remind_at"]), row["repeat"], row["timezone"], after=self.now())
                    except ValueError:
                        # Последняя представимая дата Python (год 9999): доставить
                        # конечный экземпляр вместо отката всей пачки уведомлений.
                        await connection.execute("UPDATE tasks SET repeat='none',updated_at=? WHERE id=?", (now, row["id"]))
                    else:
                        await connection.execute("UPDATE tasks SET remind_at=?,updated_at=? WHERE id=?",
                                                 (int(next_time.timestamp()), now, row["id"]))
            await self._materialize_advances(connection, now)
            return len(rows)

    async def claim_due(self, lease_seconds: int = 120) -> Delivery | None:
        now = self._timestamp()
        async with self.database.transaction() as connection:
            row = await _one(connection, DELIVERY_SELECT + """
                WHERE t.status='active' AND d.generation=t.generation AND u.blocked=0 AND d.due_at<=?
                  AND (d.kind='main' OR d.scheduled_at>?)
                  AND ((d.status='pending' AND d.next_attempt_at<=?)
                    OR (d.status='sending' AND d.lease_until<=?))
                ORDER BY d.next_attempt_at,d.id LIMIT 1
                """, (now, now, now, now))
            if row is None:
                return None
            token = uuid4().hex
            await connection.execute(
                "UPDATE deliveries SET status='sending',attempts=attempts+1,lease_until=?,claim_token=?,updated_at=? WHERE id=?",
                (now + lease_seconds, token, now, row["id"]),
            )
            return _delivery(await _one(connection, DELIVERY_SELECT + " WHERE d.id=?", (row["id"],)))

    async def claim_is_current(self, delivery: Delivery) -> bool:
        return await self.refresh_claim(delivery) is not None

    async def refresh_claim(self, delivery: Delivery) -> Delivery | None:
        async with self.database.read() as connection:
            row = await _one(connection, DELIVERY_SELECT + """
                WHERE d.id=? AND d.claim_token=? AND d.status='sending' AND d.lease_until>?
                  AND t.status='active' AND d.generation=t.generation AND u.blocked=0
                  AND (d.kind='main' OR d.scheduled_at>?)
                """, (delivery.id, delivery.claim_token, self._timestamp(), self._timestamp()))
        return _delivery(row) if row is not None else None

    async def acknowledge_delivery(self, delivery: Delivery, message_id: int) -> bool:
        now = self._timestamp()
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE deliveries SET status='sent',message_id=?,sent_at=?,lease_until=NULL,
                   claim_token=NULL,last_error=NULL,updated_at=? WHERE id=? AND claim_token=? AND status='sending'""",
                (message_id, now, now, delivery.id, delivery.claim_token),
            )
            return bool(cursor.rowcount)

    async def retry_delivery(self, delivery: Delivery, *, delay: int, reason: str,
                             blocked: bool = False) -> None:
        now = self._timestamp()
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE deliveries SET status='pending',next_attempt_at=?,lease_until=NULL,
                   claim_token=NULL,last_error=?,updated_at=? WHERE id=? AND claim_token=? AND status='sending'""",
                (now + max(1, delay), reason[:100], now, delivery.id, delivery.claim_token),
            )
            if blocked and cursor.rowcount:
                await connection.execute("UPDATE users SET blocked=1,updated_at=? WHERE id=?", (now, delivery.user_id))
