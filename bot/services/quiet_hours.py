"""Quiet settings and waking only deliveries deferred by quiet hours."""

from bot.services.task_service import TaskService,ValidationError
from bot.utils.datetime_utils import parse_time


class QuietHoursService:
    def __init__(self,tasks: TaskService):
        self.tasks=tasks

    async def configure(self,user_id: int,*,enabled: bool | None=None,start: str | None=None,
                        end: str | None=None,exempt_high: bool | None=None) -> None:
        if any(value is not None and type(value) is not bool for value in (enabled,exempt_high)):
            raise ValidationError('Некорректная настройка тихих часов.')
        async with self.tasks.task_lock(-user_id):
            user=await self.tasks.get_user(user_id)
            try:
                first=parse_time(start).strftime('%H:%M') if start is not None else user.quiet_start
                last=parse_time(end).strftime('%H:%M') if end is not None else user.quiet_end
            except ValueError as error:
                raise ValidationError(str(error)) from error
            if first==last:
                raise ValidationError('Начало и конец должны различаться. Для обычной работы выключи режим.')
            now=self.tasks._timestamp()
            async with self.tasks.database.transaction() as connection:
                await connection.execute('''UPDATE users SET quiet_enabled=?,quiet_start=?,quiet_end=?,quiet_exempt_high=?,updated_at=? WHERE id=?''',
                    (user.quiet_enabled if enabled is None else enabled,first,last,
                     user.quiet_exempt_high if exempt_high is None else exempt_high,now,user_id))
                await self.tasks._wake_quiet(connection,user_id,now)
