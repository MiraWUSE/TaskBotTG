"""Stable option keys shared by storage and Russian UI."""

PRIORITIES = {'high': '🔴 Высокий', 'medium': '🟡 Средний', 'low': '🟢 Низкий'}
CATEGORIES = {'study': '🎓 Учёба', 'work': '💼 Работа', 'personal': '🏠 Личное',
              'shopping': '🛒 Покупки', 'other': '📌 Другое'}
ADVANCES = {5: 'За 5 минут', 15: 'За 15 минут', 30: 'За 30 минут',
            60: 'За 1 час', 1440: 'За 1 день'}


def priority_icon(value: str) -> str:
    return PRIORITIES.get(value, PRIORITIES['medium']).split()[0]
