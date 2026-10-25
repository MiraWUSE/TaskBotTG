"""Read-only local calendar projections; no extra deliveries are created."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bot.services.task_service import LIVE_DELIVERIES, TASK_VIEW, TaskService, ValidationError, _all, _datetime, _task
from bot.utils.datetime_utils import _local_to_utc, next_occurrence


class CalendarService:
    def __init__(self, tasks: TaskService):
        self.tasks = tasks

    async def dates(self, user_id: int, start: date, end: date) -> dict[date, list]:
        if not isinstance(start, date) or not isinstance(end, date) or not 0 < (end-start).days <= 32:
            raise ValidationError('Выбери день или месяц календаря.')
        user = await self.tasks.get_user(user_id)
        zone = ZoneInfo(user.timezone)
        first = _local_to_utc(datetime.combine(start,time.min),zone,forward_gap=True)
        last = _local_to_utc(datetime.combine(end,time.min),zone,forward_gap=True)
        async with self.tasks.database.read() as connection:
            rows = await _all(connection, TASK_VIEW + "SELECT * FROM task_view WHERE user_id=? AND status='active'", (user_id,))
            outstanding = await _all(connection, f'''SELECT d.task_id,d.due_at FROM deliveries d JOIN tasks t ON t.id=d.task_id
                WHERE t.user_id=? AND t.status='active' AND d.kind='main' AND d.generation=t.generation
                AND d.status IN {LIVE_DELIVERIES} AND d.due_at>=? AND d.due_at<?''',
                (user_id,int(first.timestamp()),int(last.timestamp())))
        moments = {}
        for row in outstanding:
            moments.setdefault(row['task_id'],[]).append(_datetime(row['due_at']))
        result = {}
        for row in rows:
            task = _task(row)
            anchor = _datetime(row['remind_at'])
            candidates = moments.get(task.id, [])
            if first <= anchor < last:
                candidates.append(anchor)
            if task.repeat != 'none' and anchor < last:
                reference = max(anchor, first - timedelta(seconds=1))
                for _ in range(33):
                    try:
                        moment = next_occurrence(anchor, task.repeat, task.timezone, after=reference)
                    except ValueError:
                        break
                    if moment >= last:
                        break
                    if moment >= first:
                        candidates.append(moment)
                    reference = moment
            for moment in candidates:
                day = moment.astimezone(zone).date()
                if start <= day < end:
                    bucket = result.setdefault(day,{})
                    previous = bucket.get(task.id)
                    if previous is None or moment < previous.remind_at:
                        bucket[task.id] = replace(task, remind_at=moment)
        return {day: sorted(items.values(),key=lambda task:(task.remind_at,task.id)) for day,items in result.items()}

    async def day(self, user_id: int, day: date) -> list:
        return (await self.dates(user_id, day, day+timedelta(days=1))).get(day, [])
