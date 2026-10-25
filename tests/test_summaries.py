import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.database.db import Database
from bot.services.summaries import SummaryService
from bot.services.task_service import TaskService, ValidationError


class Sender:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def send_message(self, **kwargs):
        if self.fail:
            self.fail = False
            raise OSError('offline')
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=len(self.calls))


async def configure(service, *, send_empty=False):
    summaries = SummaryService(service)
    await summaries.configure(1, enabled=True, clock='13:00', send_empty=send_empty)
    return summaries


async def test_disabled_default_and_enable_future_time(service, clock):
    sender = Sender()
    summaries = SummaryService(service)
    assert not (await service.get_user(1)).summary_enabled
    assert await summaries.send_due(sender) == 0
    await summaries.configure(1, enabled=True, clock='8:00', send_empty=True)
    assert await summaries.send_due(sender) == 0  # Today's time already elapsed at enable.
    clock.advance(days=1)
    assert await summaries.send_due(sender) == 1


async def test_summary_lists_all_categories_and_only_owner_once(service, clock):
    await service.create_task(1, '<Работа>', clock() + timedelta(hours=2), priority='high', category='work')
    await service.create_task(1, 'Учёба', clock() + timedelta(hours=3), category='study')
    await service.create_task(2, 'Чужая', clock() + timedelta(hours=2))
    await service.set_category_filter(1, 'shopping')
    summaries = await configure(service)
    sender = Sender()
    clock.advance(hours=1)
    assert await summaries.send_due(sender) == 1
    assert await summaries.send_due(sender) == 0
    text = sender.calls[0]['text']
    assert 'задач: 2' in text and '&lt;Работа&gt;' in text and 'Учёба' in text and 'Чужая' not in text


@pytest.mark.parametrize('send_empty,expected', [(False,0),(True,1)])
async def test_empty_day_option(service, clock, send_empty, expected):
    summaries = await configure(service, send_empty=send_empty)
    clock.advance(hours=1)
    assert await summaries.send_due(Sender()) == expected


async def test_summary_settings_survive_restart_and_no_duplicate(tmp_path, clock):
    path = tmp_path / 'summary.sqlite3'
    sender = Sender()
    async with Database(path) as db:
        service = TaskService(db, clock=clock)
        await service.ensure_user(1,1)
        summaries = await configure(service, send_empty=True)
        clock.advance(hours=1)
        assert await summaries.send_due(sender) == 1
    async with Database(path) as db:
        service = TaskService(db, clock=clock)
        assert (await service.get_user(1)).summary_time == '13:00'
        assert await SummaryService(service).send_due(sender) == 0


async def test_retry_lease_and_expired_day_not_backfilled(service, clock):
    summaries = await configure(service, send_empty=True)
    clock.advance(hours=1)
    sender = Sender(fail=True)
    assert await summaries.send_due(sender) == 0
    clock.advance(seconds=30)
    await summaries.prepare()
    first = await summaries.claim(10)
    assert first
    clock.advance(seconds=11)
    second = await summaries.claim(10)
    assert second['id'] == first['id'] and second['claim_token'] != first['claim_token']
    clock.advance(days=1)
    assert await summaries.send_due(sender) == 1
    assert len(sender.calls) == 1


async def test_disable_or_timezone_change_invalidates_pending(service, clock):
    summaries = await configure(service, send_empty=True)
    clock.advance(hours=1)
    await summaries.prepare()
    await summaries.configure(1, enabled=False)
    assert await summaries.claim(120) is None
    await summaries.configure(1, enabled=True, clock='14:00')
    clock.advance(hours=1)
    await service.set_timezone(1, 'Asia/Tokyo')
    assert await summaries.claim(120) is None


async def test_different_timezone_near_midnight_and_dst(service, clock):
    await service.set_timezone(1, 'Asia/Tokyo')
    summaries = SummaryService(service)
    await summaries.configure(1, enabled=True, clock='1:00', send_empty=True)
    clock.advance(hours=7)  # 01:00 on the next local day.
    sender = Sender()
    assert await summaries.send_due(sender) == 1
    assert '24.10.2026' in sender.calls[0]['text']
    await service.set_timezone(1, 'Europe/Helsinki')
    clock.value = datetime(2027,3,27,1,tzinfo=UTC)
    await summaries.configure(1, clock='3:30')
    clock.value = datetime(2027,3,28,1,30,tzinfo=UTC)
    assert await summaries.send_due(sender) == 1


async def test_long_html_titles_fit_telegram_limit(service, clock):
    for i in range(20):
        await service.create_task(1, '&' * 190 + str(i), clock() + timedelta(hours=2))
    summaries = await configure(service)
    clock.advance(hours=1)
    sender = Sender()
    assert await summaries.send_due(sender) == 1
    assert len(sender.calls[0]['text']) < 4096


async def test_two_connections_claim_same_day_once(tmp_path, clock):
    path = tmp_path / 'parallel.sqlite3'
    async with Database(path) as first, Database(path) as second:
        s1, s2 = TaskService(first, clock=clock), TaskService(second, clock=clock)
        await s1.ensure_user(1,1)
        a = await configure(s1, send_empty=True)
        b = SummaryService(s2)
        clock.advance(hours=1)
        await asyncio.gather(a.prepare(),b.prepare())
        claims = await asyncio.gather(a.claim(120),b.claim(120))
        assert sum(c is not None for c in claims) == 1


async def test_invalid_clock(service):
    with pytest.raises(ValidationError):
        await SummaryService(service).configure(1, clock='25:00')
