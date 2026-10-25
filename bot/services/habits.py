"""Separate daily habits and reversible date-keyed check-ins."""

from dataclasses import dataclass
from datetime import date,timedelta
from zoneinfo import ZoneInfo

from bot.services.task_service import TaskService,TaskNotFound,ValidationError,_all,_one,_datetime


@dataclass(frozen=True)
class Habit:
    id: int
    title: str
    active: bool
    revision: int
    today: date
    checked_today: bool
    streak: int
    best: int
    total: int
    week: int
    month: int
    month_days: int
    history: tuple[str,...]


class HabitService:
    def __init__(self,tasks: TaskService):
        self.tasks=tasks

    @staticmethod
    async def _owned(connection,user_id,habit_id):
        row=await _one(connection,'SELECT * FROM habits WHERE id=? AND user_id=?',(habit_id,user_id))
        if row is None:
            raise TaskNotFound()
        return row

    async def add(self,user_id: int,title: str) -> Habit:
        title=self.tasks._title(title)
        await self.tasks.get_user(user_id)
        now=self.tasks._timestamp()
        async with self.tasks.task_lock(-user_id),self.tasks.database.transaction() as connection:
            count=(await _one(connection,'SELECT COUNT(*) FROM habits WHERE user_id=? AND active=1',(user_id,)))[0]
            if count>=50:
                raise ValidationError('Можно вести до 50 активных привычек.')
            cursor=await connection.execute('INSERT INTO habits(user_id,title,created_at,updated_at) VALUES (?,?,?,?)',(user_id,title,now,now))
            habit_id=cursor.lastrowid
        return await self.get(user_id,habit_id)

    async def list(self,user_id: int,*,archived=False,page=0,page_size=6):
        if page<0 or page>1_000_000 or not 1<=page_size<=20:
            raise ValidationError('Неверная страница.')
        await self.tasks.get_user(user_id)
        async with self.tasks.database.read() as connection:
            total=(await _one(connection,'SELECT COUNT(*) FROM habits WHERE user_id=? AND active=?',(user_id,not archived)))[0]
            rows=await _all(connection,'SELECT id FROM habits WHERE user_id=? AND active=? ORDER BY id LIMIT ? OFFSET ?',
                            (user_id,not archived,page_size,page*page_size))
        return [await self.get(user_id,r['id']) for r in rows],total

    async def get(self,user_id: int,habit_id: int) -> Habit:
        user=await self.tasks.get_user(user_id)
        zone=ZoneInfo(user.timezone)
        today=self.tasks.now().astimezone(zone).date()
        async with self.tasks.database.read() as connection:
            row=await self._owned(connection,user_id,habit_id)
            checks=await _all(connection,'SELECT local_day FROM habit_checks WHERE habit_id=? AND done=1 ORDER BY local_day',(habit_id,))
        days={date.fromisoformat(r['local_day']) for r in checks}
        eligible={d for d in days if d<=today}
        cursor=today if today in days else today-timedelta(days=1)
        streak=0
        while cursor in days:
            streak+=1
            cursor-=timedelta(days=1)
        best=run=0
        previous=None
        for day in sorted(eligible):
            run=run+1 if previous is not None and day-previous==timedelta(days=1) else 1
            best=max(best,run)
            previous=day
        first=max(today-timedelta(days=29),_datetime(row['created_at']).astimezone(zone).date())
        return Habit(row['id'],row['title'],bool(row['active']),row['revision'],today,today in days,streak,best,len(days),
                     sum(today-timedelta(days=6)<=d<=today for d in days),sum(first<=d<=today for d in days),
                     max(1,(today-first).days+1),tuple(d.isoformat() for d in sorted(days,reverse=True)[:30]))

    async def check(self,user_id: int,habit_id: int,*,done: bool=True,day: date | None=None,revision: int | None=None) -> Habit:
        if type(done) is not bool:
            raise ValidationError('Некорректная отметка.')
        async with self.tasks.task_lock(-user_id):
            user=await self.tasks.get_user(user_id)
            today=self.tasks.now().astimezone(ZoneInfo(user.timezone)).date()
            if day is not None and day!=today:
                raise ValidationError('Местный день изменился. Открой привычку заново.')
            async with self.tasks.database.transaction() as connection:
                row=await self._owned(connection,user_id,habit_id)
                if not row['active'] or (revision is not None and revision!=row['revision']):
                    raise TaskNotFound()
                await connection.execute('''INSERT INTO habit_checks(habit_id,local_day,timezone,checked_at,done) VALUES (?,?,?,?,?)
                    ON CONFLICT(habit_id,local_day) DO UPDATE SET done=excluded.done,checked_at=excluded.checked_at,timezone=excluded.timezone''',
                    (habit_id,today.isoformat(),user.timezone,self.tasks._timestamp(),done))
                await connection.execute('UPDATE habits SET revision=revision+1,updated_at=? WHERE id=?',(self.tasks._timestamp(),habit_id))
        return await self.get(user_id,habit_id)

    async def change(self,user_id: int,habit_id: int,*,title: str | None=None,active: bool | None=None,revision: int | None=None) -> Habit:
        if title is not None:
            title=self.tasks._title(title)
        async with self.tasks.task_lock(-user_id),self.tasks.database.transaction() as connection:
            row=await self._owned(connection,user_id,habit_id)
            if revision is not None and revision!=row['revision']:
                raise TaskNotFound()
            if active and not row['active']:
                count=(await _one(connection,'SELECT COUNT(*) FROM habits WHERE user_id=? AND active=1',(user_id,)))[0]
                if count>=50:
                    raise ValidationError('Можно вести до 50 активных привычек.')
            await connection.execute('UPDATE habits SET title=?,active=?,revision=revision+1,updated_at=? WHERE id=?',
                (row['title'] if title is None else title,row['active'] if active is None else active,self.tasks._timestamp(),habit_id))
        return await self.get(user_id,habit_id)
