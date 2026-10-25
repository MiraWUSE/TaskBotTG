"""Bounded, deterministic Russian task parser; no network or guessing of dates."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.utils.datetime_utils import combine_local, parse_time, parse_user_date

WEEKDAYS = {
    'понедельник': 0, 'вторник': 1, 'среду': 2, 'четверг': 3,
    'пятницу': 4, 'субботу': 5, 'воскресенье': 6,
}
DATE = r'(?:сегодня|завтра|послезавтра|[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[./-][0-9]{1,2}(?:[./-][0-9]{4})?|в\s+(?:' + '|'.join(WEEKDAYS) + r'))'
CLOCK = r'[0-9]{1,2}(?:\s*[:.]\s*[0-9]{1,2})?'
ABSOLUTE = re.compile(rf'^(?P<day>{DATE})\s+(?:в\s+)?(?P<clock>{CLOCK})(?:\s+(?P<period>утра|дня|вечера|ночи))?\s+(?P<title>.+)$', re.I)
RELATIVE = re.compile(r'^через\s+(?P<count>[0-9]+)\s+(?P<unit>минуту|минуты|минут|час|часа|часов|день|дня|дней)\s+(?P<title>.+)$', re.I)
LOOKS_LIKE_TASK = re.compile(rf'^(?:напомни\b|через\b|{DATE}\b|в\s+{CLOCK}\b)', re.I)


@dataclass(frozen=True)
class QuickTask:
    title: str
    moment: datetime | None
    clarification: str | None = None


def _title(text: str) -> str:
    title = ' '.join(text.split())
    if not title or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise ValueError('Название должно содержать от 1 до 200 символов.')
    return title[0].upper() + title[1:]


def parse_quick_task(text: str, timezone: str, now: datetime) -> QuickTask | None:
    if not isinstance(text, str) or not LOOKS_LIKE_TASK.match(text.strip()):
        return None
    if len(text) > 512:
        raise ValueError('Сообщение слишком длинное. Название задачи — до 200 символов.')
    text = re.sub(r'^напомни\s+', '', text.strip(), flags=re.I)
    match = RELATIVE.fullmatch(text)
    if match:
        count = int(match['count'])
        unit = match['unit'].lower()
        minutes = count * (1 if unit.startswith('минут') else 60 if unit.startswith('час') else 1440)
        if not 0 < minutes <= 5256000:
            raise ValueError('Укажи положительный интервал не более 10 лет.')
        try:
            return QuickTask(_title(match['title']), now + timedelta(minutes=minutes))
        except OverflowError as error:
            raise ValueError('Дата за пределами допустимого диапазона.') from error
    match = ABSOLUTE.fullmatch(text)
    if match:
        if match['title'].strip().casefold() in {'утра', 'дня', 'вечера', 'ночи'}:
            raise ValueError('Добавь название задачи после времени.')
        title = _title(match['title'])
        clock = parse_time(match['clock'])
        period = (match['period'] or '').lower()
        hour = clock.hour
        if period:
            if period == 'утра' and hour <= 11:
                pass
            elif period in {'дня', 'вечера'} and 1 <= hour <= 12:
                hour = hour % 12 + 12
            elif period == 'ночи' and (hour <= 4 or hour == 12):
                hour %= 12
            else:
                raise ValueError('Уточни время в 24-часовом формате, например 21:00.')
            clock = clock.replace(hour=hour)
        day_text = match['day'].lower()
        if day_text.startswith('в '):
            weekday = WEEKDAYS[day_text.split()[-1]]
            local = now.astimezone(ZoneInfo(timezone))
            shift = (weekday - local.weekday()) % 7
            day = local.date() + timedelta(days=shift)
            if shift == 0 and clock <= local.time().replace(tzinfo=None):
                day += timedelta(days=7)
        elif re.fullmatch(r'[0-9]{1,2}[./-][0-9]{1,2}', day_text):
            return QuickTask(title, None, 'Уточни год: введи полную дату и время, например 25.11.2026 14:30.')
        else:
            day = parse_user_date(day_text, timezone, now)
        return QuickTask(title, combine_local(day, clock, timezone, now))
    missing_date = re.fullmatch(rf'в\s+{CLOCK}\s+(.+)', text, re.I)
    missing_time = re.fullmatch(rf'{DATE}\s+(.+)', text, re.I)
    match = missing_date or missing_time
    if match:
        if re.match(r'^(?:в\s+)?[0-9]', match[1], re.I):
            raise ValueError('Добавь название задачи после корректного времени.')
        # Preserve the title, but ask explicitly instead of choosing a date/time.
        return QuickTask(_title(match[1]), None, 'Уточни дату и время задачи. Можно сначала отправить дату, затем время.')
    raise ValueError('Не удалось выделить дату, время и название. Например: «Завтра в 15:00 сходить в магазин».')
