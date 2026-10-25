"""Opt-in daily digests with a durable per-local-day delivery lease."""

import asyncio
import logging
from datetime import datetime, time
from html import escape
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot.services.task_service import TaskService, ValidationError, _all, _one
from bot.task_options import priority_icon
from bot.utils.datetime_utils import _local_to_utc, parse_time
from bot.utils.quiet_hours import quiet_until

logger = logging.getLogger(__name__)


class SummaryService:
    def __init__(self, tasks: TaskService):
        self.tasks = tasks

    async def configure(self, user_id: int, *, enabled: bool | None = None,
                        clock: str | None = None, send_empty: bool | None = None) -> None:
        async with self.tasks.task_lock(-user_id):
            user = await self.tasks.get_user(user_id)
            try:
                value = parse_time(clock).strftime('%H:%M') if clock is not None else user.summary_time
            except ValueError as error:
                raise ValidationError(str(error)) from error
            async with self.tasks.database.transaction() as connection:
                await connection.execute("""UPDATE users SET summary_enabled=?,summary_time=?,summary_empty=?,
                    summary_from=?,updated_at=? WHERE id=?""",
                    (user.summary_enabled if enabled is None else enabled,value,
                     user.summary_empty if send_empty is None else send_empty,
                     self.tasks._timestamp(),self.tasks._timestamp(),user_id))
                # Do not send queued messages prepared with old settings.
                await connection.execute("UPDATE daily_summaries SET status='skipped',claim_token=NULL,lease_until=NULL WHERE user_id=? AND status IN ('pending','sending')",(user_id,))

    async def prepare(self) -> None:
        now = self.tasks.now()
        async with self.tasks.database.read() as connection:
            users = await _all(connection, 'SELECT * FROM users WHERE summary_enabled=1 AND blocked=0')
        for user in users:
            zone = ZoneInfo(user['timezone'])
            local_day = now.astimezone(zone).date()
            # A missing wall-clock time moves to the first valid time after the gap.
            due = _local_to_utc(datetime.combine(local_day, time.fromisoformat(user['summary_time'])), zone, forward_gap=True)
            stamp = int(due.timestamp())
            if due > now or stamp <= user['summary_from']:
                continue
            tasks, total = await self.tasks.list_tasks(user['id'], today=True, page_size=20, category='all', sort_by='time')
            lines = [f'☀️ <b>Доброе утро!</b>\n\n📅 {local_day.strftime("%d.%m.%Y")}\nНа сегодня задач: {total}']
            shown = 0
            for task in tasks[:15]:
                line = f'{priority_icon(task.priority)} {task.remind_at.astimezone(zone):%H:%M} — {escape(task.title[:100])}'
                if sum(map(len, lines)) + len(line) > 3400:
                    break
                lines.append(line)
                shown += 1
            if total > shown:
                lines.append(f'Ещё {total - shown} — в /today.')
            if total == 0:
                lines.append('Сегодня свободный день ✨')
            async with self.tasks.database.transaction() as connection:
                current = await _one(connection, 'SELECT * FROM users WHERE id=?', (user['id'],))
                if (not current['summary_enabled'] or current['summary_from'] != user['summary_from']
                        or current['timezone'] != user['timezone'] or current['blocked']
                        or current['summary_time'] != user['summary_time'] or current['summary_empty'] != user['summary_empty']):
                    continue
                await connection.execute('''INSERT OR IGNORE INTO daily_summaries(user_id,local_day,scheduled_at,
                    text,status,next_attempt_at,timezone) VALUES (?,?,?,?,?,?,?)''',
                    (user['id'], local_day.isoformat(), stamp, '\n'.join(lines),
                     'pending' if total or user['summary_empty'] else 'skipped', stamp, user['timezone']))

    async def claim(self, lease_seconds: int):
        now = self.tasks._timestamp()
        async with self.tasks.database.transaction() as connection:
            rows = await _all(connection, '''SELECT d.*,u.chat_id,u.timezone AS user_timezone FROM daily_summaries d
                JOIN users u ON u.id=d.user_id WHERE u.summary_enabled=1 AND u.blocked=0
                AND ((d.status='pending' AND d.next_attempt_at<=?) OR (d.status='sending' AND d.lease_until<=?))
                ORDER BY d.scheduled_at,d.id LIMIT 50''', (now, now))
            for row in rows:
                today = self.tasks.now().astimezone(ZoneInfo(row['user_timezone'])).date().isoformat()
                if row['local_day'] != today or row['timezone'] != row['user_timezone']:
                    await connection.execute("UPDATE daily_summaries SET status='skipped',claim_token=NULL,lease_until=NULL WHERE id=?", (row['id'],))
                    continue
                token = uuid4().hex
                await connection.execute("UPDATE daily_summaries SET status='sending',attempts=attempts+1,lease_until=?,claim_token=? WHERE id=?",
                                         (now + lease_seconds, token, row['id']))
                result = dict(row)
                result.update(claim_token=token, attempts=row['attempts'] + 1)
                return result
        return None

    async def send_due(self, bot, *, lease_seconds: int = 120, limit: int = 50) -> int:
        await self.prepare()
        sent = 0
        for _ in range(limit):
            row = await self.claim(lease_seconds)
            if row is None:
                break
            async with self.tasks.task_lock(-row['user_id']):
                sent += await self._send_claim(bot,row)
        return sent

    async def _send_claim(self, bot, row) -> int:
        async with self.tasks.database.read() as connection:
            current = await _one(connection, """SELECT d.id FROM daily_summaries d JOIN users u ON u.id=d.user_id
                WHERE d.id=? AND d.claim_token=? AND d.status='sending' AND d.lease_until>?
                AND u.summary_enabled=1 AND u.blocked=0 AND u.timezone=d.timezone""",
                (row['id'],row['claim_token'],self.tasks._timestamp()))
        today = self.tasks.now().astimezone(ZoneInfo(row['timezone'])).date().isoformat()
        if current is None or row['local_day'] != today:
            return 0
        user=await self.tasks.get_user(row['user_id'])
        until=quiet_until(user,self.tasks.now())
        if until is not None:
            stamp=int(until.timestamp())
            async with self.tasks.database.transaction() as connection:
                await connection.execute('''UPDATE daily_summaries SET status='pending',next_attempt_at=?,quiet_until=?,
                    attempts=MAX(0,attempts-1),lease_until=NULL,claim_token=NULL WHERE id=? AND claim_token=? AND status='sending' ''',
                    (stamp,stamp,row['id'],row['claim_token']))
            return 0
        try:
            message = await bot.send_message(chat_id=row['chat_id'],text=row['text'],parse_mode='HTML')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            delay = min(3600,30 * 2 ** min(row['attempts']-1,7))
            if isinstance(error,TelegramRetryAfter):
                delay = max(delay,int(error.retry_after)+1)
            async with self.tasks.database.transaction() as connection:
                cursor = await connection.execute("UPDATE daily_summaries SET status='pending',next_attempt_at=?,claim_token=NULL,lease_until=NULL WHERE id=? AND claim_token=? AND status='sending'",
                                                  (self.tasks._timestamp()+delay,row['id'],row['claim_token']))
                if isinstance(error,TelegramForbiddenError) and cursor.rowcount:
                    await connection.execute('UPDATE users SET blocked=1 WHERE id=?',(row['user_id'],))
            logger.warning('Сводка %s отложена: %s',row['id'],type(error).__name__)
            return 0
        async with self.tasks.database.transaction() as connection:
            cursor = await connection.execute("UPDATE daily_summaries SET status='sent',message_id=?,sent_at=?,claim_token=NULL,lease_until=NULL WHERE id=? AND claim_token=? AND status='sending'",
                                              (message.message_id,self.tasks._timestamp(),row['id'],row['claim_token']))
            return int(bool(cursor.rowcount))
