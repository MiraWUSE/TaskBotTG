"""Completion events and outstanding occurrences, grouped in the user's zone."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from bot.services.task_service import TaskService, ValidationError, _all
from bot.utils.datetime_utils import _local_to_utc


@dataclass(frozen=True)
class Statistics:
    today: int
    week: int
    month: int
    overdue: int
    due_completed: int
    due_total: int
    percent: int | None


class StatisticsService:
    def __init__(self,tasks: TaskService):
        self.tasks = tasks

    async def get(self,user_id: int,period: str = 'week') -> Statistics:
        if period not in {'day','week','month'}:
            raise ValidationError('Выбери день, неделю или месяц.')
        user = await self.tasks.get_user(user_id)
        zone = ZoneInfo(user.timezone)
        now = self.tasks.now()
        day = now.astimezone(zone).date()
        beginnings = {'day':day,'week':day-timedelta(days=day.weekday()),'month':day.replace(day=1)}
        starts = {key:int(_local_to_utc(datetime.combine(value,time.min),zone,forward_gap=True).timestamp())
                  for key,value in beginnings.items()}
        stamp = int(now.timestamp())
        async with self.tasks.database.read() as connection:
            completed = await _all(connection,'''SELECT * FROM task_completions WHERE user_id=? AND completed_at<=?
                AND (completed_at>=? OR scheduled_at>=?)''',(user_id,stamp,min(starts.values()),starts[period]))
            overdue = await _all(connection,'''SELECT t.id AS task_id,t.generation,t.remind_at AS scheduled_at
                FROM tasks t WHERE t.user_id=? AND t.status='active' AND t.remind_at<=?
                AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.task_id=t.id AND d.generation=t.generation
                    AND d.kind='main' AND d.scheduled_at=t.remind_at)
                UNION
                SELECT t.id,t.generation,d.scheduled_at FROM deliveries d JOIN tasks t ON t.id=d.task_id
                WHERE t.user_id=? AND t.status='active' AND d.generation=t.generation AND d.kind='main'
                AND d.status IN ('pending','sending','sent') AND d.due_at<=?''',(user_id,stamp,user_id,stamp))
        counts = {key:sum(start<=r['completed_at']<=stamp for r in completed) for key,start in starts.items()}
        cutoff = starts[period]
        due_completed = {(r['task_id'],r['generation'],r['scheduled_at']) for r in completed
                         if r['scheduled_at'] is not None and cutoff<=r['scheduled_at']<=stamp}
        due_overdue = {(r['task_id'],r['generation'],r['scheduled_at']) for r in overdue
                      if cutoff<=r['scheduled_at']<=stamp}
        total = len(due_completed | due_overdue)
        return Statistics(counts['day'],counts['week'],counts['month'],len(overdue),len(due_completed),total,
                          round(100*len(due_completed)/total) if total else None)
