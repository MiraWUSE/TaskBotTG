"""UTC storage and local calendar arithmetic for reminder dates.

Ambiguous local times (when clocks move back) use the first occurrence,
``fold=0``. User input in a spring-forward gap is rejected. Recurrences skip
a nonexistent local date rather than changing the series' wall-clock time.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DATE_HELP = "Укажите дату, например 24.10.2026, 24/10/2026 или 2026-10-24."
_TIME_HELP = "Укажите время, например 8:00, 8:5, 8.00 или просто 8 (08:00)."
_DATETIME_HELP = "Укажите дату и время, например 24.10.2026 8:00 или завтра в 8:00."
_DAY_FIRST_DATE = re.compile(
    r"(?P<day>[0-9]{1,2})(?P<separator>[./-])"
    r"(?P<month>[0-9]{1,2})(?P=separator)(?P<year>[0-9]{4})"
)
_ISO_DATE = re.compile(r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})")
_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_DATE_TOKEN = (
    r"(?:[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{4}"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}|сегодня|завтра|послезавтра)"
)
_DATE_ONLY = re.compile(_DATE_TOKEN, re.IGNORECASE)
_COMBINED_DATETIME = re.compile(
    rf"(?P<day>{_DATE_TOKEN})(?:\s*[Tt]\s*|\s+(?:в\s+)?)(?P<clock>.+)",
    re.IGNORECASE | re.DOTALL,
)
_CLOCK = re.compile(
    r"(?P<hour>[0-9]{1,2})(?:\s*(?::\s*(?P<colon_minutes>[0-9]{1,2})"
    r"|\.\s*(?P<dot_minutes>[0-9]{2})))?"
)


def _input_text(text: str, limit: int, help_text: str) -> str:
    if not isinstance(text, str) or len(text) > limit or not text.strip():
        raise ValueError(help_text)
    return text.strip()


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


def validate_timezone(name: str) -> str:
    """Return a stripped IANA zone name, or a user-readable error."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Укажите часовой пояс, например Europe/Moscow.")
    normalized = name.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(
            "Неизвестный часовой пояс. Используйте IANA-имя, например Europe/Moscow."
        ) from exc
    return normalized


def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(validate_timezone(name))


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Дата и время должны содержать часовой пояс.")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Дата находится за пределами поддерживаемого диапазона.") from exc


def _local_to_utc(value: datetime, zone: ZoneInfo, *, forward_gap: bool = False) -> datetime:
    """Resolve a naive local datetime with a round trip through UTC.

``replace(tzinfo=...)`` alone accepts nonexistent local times, so both folds
are round-tripped. The first valid fold is the documented ambiguity policy.
Day boundaries may move forward across a midnight gap to the day's first
existing local time; manual input and recurrence candidates must exist.
"""
    shifted: list[tuple[datetime, datetime]] = []
    for fold in (0, 1):
        try:
            candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            round_trip = candidate.astimezone(zone).replace(tzinfo=None)
        except (OverflowError, ValueError) as exc:
            raise ValueError("Дата находится за пределами поддерживаемого диапазона.") from exc
        if round_trip == value:
            return candidate
        if round_trip > value:
            shifted.append((round_trip, candidate))
    if forward_gap and shifted:
        return min(shifted, key=lambda item: item[0])[1]
    raise ValueError(
        "Это местное время не существует из-за перевода часов. Укажите другое время."
    )


def parse_date(text: str) -> date:
    """Parse D.M.YYYY, D/M/YYYY, D-M-YYYY or ISO YYYY-MM-DD.

    Day and month may have one or two digits in day-first notation. The
    separator must be consistent and the four-digit year is always required.
    Month-first dates and omitted years are never inferred.
    """
    text = _input_text(text, 32, _DATE_HELP)
    match = _DAY_FIRST_DATE.fullmatch(text) or _ISO_DATE.fullmatch(text)
    if match is None:
        raise ValueError(_DATE_HELP)
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError as exc:
        raise ValueError("Такой даты не существует. " + _DATE_HELP) from exc


def parse_user_date(text: str, timezone: str, now: datetime | None = None) -> date:
    """Parse a calendar date or today/tomorrow/day-after in the user's zone.

    Russian relative words are case-insensitive. This helper deliberately
    accepts a date without a time for the creation dialog's separate step.
    It does not enforce a future date; ``combine_local`` validates the final
    instant once a clock time is supplied.
    """
    text = _input_text(text, 32, _DATE_HELP + " Также можно написать сегодня, завтра или послезавтра.")
    shift = _RELATIVE_DAYS.get(text.casefold())
    if shift is None:
        return parse_date(text)
    zone = _zone(timezone)
    reference = utc_now() if now is None else _aware_utc(now)
    try:
        return reference.astimezone(zone).date() + timedelta(days=shift)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Дата находится за пределами поддерживаемого диапазона.") from exc


def parse_time(text: str) -> time:
    """Parse H:M, H:MM, H.MM or a lone hour H (meaning H:00).

    Hours may have one or two digits; dot notation requires two minute
    digits so that a decimal such as 8.5 is not silently interpreted.
    Whitespace around ':' or '.' is allowed. Seconds and AM/PM are rejected.
    """
    text = _input_text(text, 32, _TIME_HELP)
    match = _CLOCK.fullmatch(text)
    if match is None:
        raise ValueError(_TIME_HELP)
    minute_text = match["colon_minutes"] or match["dot_minutes"] or "0"
    try:
        return time(int(match["hour"]), int(minute_text))
    except ValueError as exc:
        raise ValueError("Некорректное время. Укажите часы от 00 до 23 и минуты от 00 до 59.") from exc


def combine_local(
    day: date, clock: time, timezone: str, now: datetime | None = None
) -> datetime:
    """Convert a future local date and time to UTC, rejecting DST gaps."""
    if not isinstance(day, date) or isinstance(day, datetime) or not isinstance(clock, time):
        raise ValueError("Укажите корректные дату и время.")
    if clock.tzinfo is not None:
        raise ValueError("Время указывается без часового пояса; выберите его в настройках.")
    result = _local_to_utc(datetime.combine(day, clock).replace(fold=0), _zone(timezone))
    reference = utc_now() if now is None else _aware_utc(now)
    if result <= reference:
        raise ValueError("Нельзя поставить напоминание на прошедшее время. Выберите будущие дату и время.")
    return result


def parse_datetime(text: str, timezone: str, now: datetime | None = None) -> datetime:
    """Parse an explicit/relative local date plus clock time and return UTC.

    Accepts the date/time forms documented by ``parse_date``/``parse_time``;
    Unicode whitespace or ``T`` separates them. Optional Russian ``в`` may
    precede the time. Relative dates use one shared reference instant in the
    user's timezone, including near midnight. Inputs are bounded to 128 chars.
    """
    text = _input_text(text, 128, _DATETIME_HELP)
    reference = utc_now() if now is None else _aware_utc(now)
    match = _COMBINED_DATETIME.fullmatch(text)
    if match is None:
        if _DATE_ONLY.fullmatch(text):
            # Keep an impossible date's useful calendar error even when the
            # caller will next try the date-only creation step.
            parse_user_date(text, timezone, reference)
        raise ValueError(_DATETIME_HELP)
    day = parse_user_date(match["day"], timezone, reference)
    return combine_local(day, parse_time(match["clock"]), timezone, reference)


def local_day_bounds(timezone: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return today's half-open [start, end) UTC range in a user's zone."""
    zone = _zone(timezone)
    reference = utc_now() if now is None else _aware_utc(now)
    day = reference.astimezone(zone).date()
    start = _local_to_utc(datetime.combine(day, time.min), zone, forward_gap=True)
    end = _local_to_utc(datetime.combine(day + timedelta(days=1), time.min), zone, forward_gap=True)
    return start, end


def next_occurrence(
    scheduled_at: datetime, repeat: str, timezone: str, after: datetime
) -> datetime:
    """Return the first following recurrence strictly later than ``after``.

    Daily/weekday recurrences preserve the local clock; weekly recurrences
    also preserve the weekday. Missed periods are skipped in one calendar
    jump. If a local timestamp does not exist on a DST transition date, that
    occurrence is skipped (weekly: a whole week); the original clock remains
    the anchor for future occurrences. An ambiguous time uses ``fold=0``.
    """
    if repeat not in {"daily", "weekdays", "weekly"}:
        raise ValueError("Неизвестный тип повторения напоминания.")
    zone = _zone(timezone)
    scheduled_utc = _aware_utc(scheduled_at)
    reference = max(_aware_utc(after), scheduled_utc)
    local = scheduled_utc.astimezone(zone)
    elapsed_days = (reference.astimezone(zone).date() - local.date()).days
    step = 7 if repeat == "weekly" else 1
    jumps = max(1, elapsed_days // step)
    try:
        candidate_day = local.date() + timedelta(days=jumps * step)
        while True:
            if repeat != "weekdays" or candidate_day.weekday() < 5:
                try:
                    candidate = _local_to_utc(
                        datetime.combine(candidate_day, local.time()).replace(fold=0), zone
                    )
                except ValueError:
                    candidate = None
                if candidate is not None and candidate > reference:
                    return candidate
            candidate_day += timedelta(days=step)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Не удалось вычислить следующую дату напоминания.") from exc


def format_datetime(value: datetime, timezone: str) -> str:
    """Format an aware timestamp in the user's local timezone."""
    return _aware_utc(value).astimezone(_zone(timezone)).strftime("%d.%m.%Y %H:%M")
