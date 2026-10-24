"""Объекты предметной области. Все datetime здесь — aware UTC."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class User:
    id: int
    chat_id: int
    timezone: str
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class Task:
    id: int
    user_id: int
    title: str
    remind_at: datetime
    repeat: str
    status: str
    timezone: str
    created_at: datetime
    updated_at: datetime
    generation: int
    revision: int = 0


@dataclass(frozen=True, slots=True)
class Delivery:
    id: int
    task_id: int
    user_id: int
    title: str
    repeat: str
    timezone: str
    scheduled_at: datetime
    due_at: datetime
    status: str
    attempts: int
    claim_token: str | None
    message_id: int | None
    chat_id: int
