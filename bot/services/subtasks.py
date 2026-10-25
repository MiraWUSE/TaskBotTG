"""Owned checklists; completing children never completes their parent."""

from bot.models import Subtask
from bot.services.task_service import TaskNotFound, TaskService, ValidationError, _all, _one


def _subtask(row) -> Subtask:
    return Subtask(row['id'],row['task_id'],row['title'],bool(row['done']),row['revision'])


class SubtaskService:
    def __init__(self, tasks: TaskService):
        self.tasks = tasks

    async def list(self, user_id: int, task_id: int) -> list[Subtask]:
        async with self.tasks.database.read() as connection:
            await self.tasks._owned_task(connection,user_id,task_id)
            rows = await _all(connection,'SELECT * FROM subtasks WHERE task_id=? AND deleted=0 ORDER BY id',(task_id,))
        return [_subtask(row) for row in rows]

    async def get(self, user_id: int, subtask_id: int) -> Subtask:
        async with self.tasks.database.read() as connection:
            row = await _one(connection,'''SELECT s.* FROM subtasks s JOIN tasks t ON t.id=s.task_id
                WHERE s.id=? AND s.deleted=0 AND t.user_id=? AND t.status='active' ''',(subtask_id,user_id))
        if row is None:
            raise TaskNotFound()
        return _subtask(row)

    async def add(self, user_id: int, task_id: int, title: str) -> Subtask:
        title = self.tasks._title(title)
        now = self.tasks._timestamp()
        async with self.tasks.task_lock(task_id), self.tasks.database.transaction() as connection:
            await self.tasks._owned_task(connection,user_id,task_id)
            count = (await _one(connection,'SELECT COUNT(*) FROM subtasks WHERE task_id=? AND deleted=0',(task_id,)))[0]
            if count >= 100:
                raise ValidationError('В одной задаче можно создать до 100 подзадач.')
            cursor = await connection.execute('INSERT INTO subtasks(task_id,title,created_at,updated_at) VALUES (?,?,?,?)',
                                              (task_id,title,now,now))
            await connection.execute('UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=?',(now,task_id))
            return _subtask(await _one(connection,'SELECT * FROM subtasks WHERE id=?',(cursor.lastrowid,)))

    async def change(self, user_id: int, subtask_id: int, *, title: str | None = None,
                     done: bool | None = None, deleted: bool = False, revision: int | None = None) -> None:
        item = await self.get(user_id,subtask_id)
        if title is not None:
            title = self.tasks._title(title)
        if done is not None and type(done) is not bool:
            raise ValidationError('Некорректная отметка выполнения.')
        now = self.tasks._timestamp()
        async with self.tasks.task_lock(item.task_id), self.tasks.database.transaction() as connection:
            await self.tasks._owned_task(connection,user_id,item.task_id)
            row = await _one(connection,'SELECT * FROM subtasks WHERE id=? AND deleted=0',(subtask_id,))
            if row is None or (revision is not None and row['revision'] != revision):
                raise TaskNotFound()
            await connection.execute('UPDATE subtasks SET title=?,done=?,deleted=?,revision=revision+1,updated_at=? WHERE id=?',
                                     (row['title'] if title is None else title,row['done'] if done is None else done,deleted,now,subtask_id))
            await connection.execute('UPDATE tasks SET revision=revision+1,updated_at=? WHERE id=?',(now,item.task_id))
