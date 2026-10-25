"""Stable option keys shared by storage and Russian UI."""

PRIORITIES = {'high': '🔴 Высокий', 'medium': '🟡 Средний', 'low': '🟢 Низкий'}


def priority_icon(value: str) -> str:
    return PRIORITIES.get(value, PRIORITIES['medium']).split()[0]
