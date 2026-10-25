from datetime import UTC, datetime

import pytest

from bot.utils.quick_task import parse_quick_task

NOW = datetime(2026, 10, 25, 9, tzinfo=UTC)


@pytest.mark.parametrize('text,expected,title', [
    ('Завтра в 15:00 сходить в магазин', '2026-10-26T12:00:00+00:00', 'Сходить в магазин'),
    ('Через 30 минут проверить почту', '2026-10-25T09:30:00+00:00', 'Проверить почту'),
    ('Сегодня в 20:00 сделать домашку', '2026-10-25T17:00:00+00:00', 'Сделать домашку'),
    ('25.11.2026 в 14:30 сходить к врачу', '2026-11-25T11:30:00+00:00', 'Сходить к врачу'),
    ('Послезавтра в 9 утра позвонить другу', '2026-10-27T06:00:00+00:00', 'Позвонить другу'),
    ('В понедельник в 8:00 тренировка', '2026-10-26T05:00:00+00:00', 'Тренировка'),
    ('напомни завтра в 8.05 дело', '2026-10-26T05:05:00+00:00', 'Дело'),
    ('завтра в 9 вечера дело', '2026-10-26T18:00:00+00:00', 'Дело'),
])
def test_supported_forms(text, expected, title):
    draft = parse_quick_task(text, 'Europe/Moscow', NOW)
    assert draft.moment == datetime.fromisoformat(expected)
    assert draft.title == title


@pytest.mark.parametrize('text', ['через 0 минут дело', 'сегодня в 8 дело', '31.11.2026 в 8 дело',
                                      'завтра в 25:00 дело', 'завтра в 8:99 дело', 'завтра в 8:00', 'через 10 минут',
                                      'завтра в 8.5 дело', 'завтра в 20 утра дело', 'завтра в 9 утра'])
def test_invalid_not_saved(text):
    with pytest.raises(ValueError):
        parse_quick_task(text, 'Europe/Moscow', NOW)


@pytest.mark.parametrize('text', ['в 8:00 купить молоко', '25.11 в 9 дело', 'завтра купить молоко'])
def test_ambiguous_requests_clarification(text):
    draft = parse_quick_task(text, 'Europe/Moscow', NOW)
    assert draft.moment is None and draft.clarification


@pytest.mark.parametrize('text', ['Привет!', 'купить молоко', '/start', 'я завтра в 8 пойду гулять'])
def test_unrelated_text(text):
    assert parse_quick_task(text, 'Europe/Moscow', NOW) is None


def test_timezone_and_dst_gap():
    draft = parse_quick_task('завтра в 8 дело', 'Asia/Tokyo', NOW)
    assert draft.moment == datetime(2026, 10, 25, 23, tzinfo=UTC)
    with pytest.raises(ValueError, match='не существует'):
        parse_quick_task('28.03.2027 в 3:30 дело', 'Europe/Helsinki', NOW)
