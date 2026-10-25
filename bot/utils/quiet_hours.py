"""Resolve local quiet intervals as UTC ranges, including DST gaps/folds."""

from datetime import UTC,datetime,time,timedelta
from zoneinfo import ZoneInfo

from bot.utils.datetime_utils import _local_to_utc


def _boundary(day,clock,zone,*,end=False):
    naive=datetime.combine(day,clock)
    result=_local_to_utc(naive,zone,forward_gap=True)
    if end:
        later=naive.replace(tzinfo=zone,fold=1).astimezone(UTC)
        if later.astimezone(zone).replace(tzinfo=None)==naive:
            result=max(result,later)
    return result


def quiet_until(user,now: datetime,*,priority: str | None=None) -> datetime | None:
    if not user.quiet_enabled or (priority=='high' and user.quiet_exempt_high):
        return None
    start,end=time.fromisoformat(user.quiet_start),time.fromisoformat(user.quiet_end)
    zone=ZoneInfo(user.timezone)
    day=now.astimezone(zone).date()
    for first in (day-timedelta(days=1),day):
        last=first+timedelta(days=1) if start>=end else first
        lower=_boundary(first,start,zone)
        upper=_boundary(last,end,zone,end=True)
        if lower<=now<upper:
            return upper
    return None
