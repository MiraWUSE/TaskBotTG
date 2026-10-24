from datetime import UTC, date, datetime, time, timedelta
from enum import Enum

import pytest

from bot.utils.datetime_utils import (
    combine_local,
    format_datetime,
    local_day_bounds,
    next_occurrence,
    parse_date,
    parse_datetime,
    parse_time,
    parse_user_date,
    utc_now,
    validate_timezone,
)


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_utc_now_is_aware():
    assert utc_now().tzinfo is UTC


@pytest.mark.parametrize(
    "text",
    [
        "31.02.2026", "29.02.2025", "00.01.2026", "01.13.2026",
        "10/24/2026", "1/2-2026", "1.2/2026", "24.10", "1.2.26",
        "2026-1-02", "2026/01/02", "01.01.0000", "24 октября 2026",
        "1.1.2026 extra", "сегодня", "", None,
    ],
)
def test_invalid_dates(text):
    with pytest.raises(ValueError):
        parse_date(text)


def test_leap_day_and_surrounding_whitespace():
    assert parse_date(" 29.02.2028 ") == date(2028, 2, 29)


@pytest.mark.parametrize(
    "text",
    ["2.1.2026", "02.01.2026", "2/1/2026", "02/01/2026", "2-1-2026", "2026-01-02"],
)
def test_flexible_dates_keep_day_first_except_iso(text):
    assert parse_date(text) == date(2026, 1, 2)


def test_slash_dates_do_not_guess_us_month_first_order():
    assert parse_date("1/2/2026") == date(2026, 2, 1)


@pytest.mark.parametrize(
    "text",
    [
        "24:00", "12:60", "-1:00", "09:000", "09:00:00", "008:00",
        "8.5", "8.", "8:", ":05", "8,5", "8 05", "8 PM", "8 часов", "24", "", None,
    ],
)
def test_invalid_times(text):
    with pytest.raises(ValueError):
        parse_time(text)


def test_valid_time():
    assert parse_time(" 09:05 ") == time(9, 5)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8:00", time(8)), ("08:00", time(8)), ("8:5", time(8, 5)),
        ("08:5", time(8, 5)), ("8.05", time(8, 5)), ("08.05", time(8, 5)),
        ("8 : 05", time(8, 5)), ("8 . 05", time(8, 5)), ("8\u00a0:\u00a05", time(8, 5)),
        ("8", time(8)), ("08", time(8)), ("0", time(0)), ("23:59", time(23, 59)),
    ],
)
def test_flexible_clock_forms(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "01.01.2026", "2026-01-02", "8:00", "сегодня", "завтра",
        "02.01.2026T", "02.01.2026 12:00 extra", "через час", "tomorrow 8:00",
        "02.01.26 8:00", "02.01.2026 в", "", None,
    ],
)
def test_datetime_requires_date_and_time(text):
    with pytest.raises(ValueError):
        parse_datetime(text, "Europe/Moscow", NOW)


@pytest.mark.parametrize(
    "text",
    [
        "24.10.2026 8:00", "24/10/2026 08:00", "24-10-2026 8.00",
        "2026-10-24 8", "2026-10-24T08:00", "2026-10-24t8:0",
        "24.10.2026T8:00", "24.10.2026    8:00", "24.10.2026\u00a08:00",
        "24.10.2026\n8:00", "24.10.2026\t8:00", "24.10.2026 в 8:00",
        "24.10.2026 В 8 . 00", " 24.10.2026 8 : 0 ",
    ],
)
def test_flexible_combined_dates_and_clocks(text):
    assert parse_datetime(text, "Europe/Moscow", NOW) == datetime(2026, 10, 24, 5, tzinfo=UTC)


def test_combined_date_allows_single_digit_day_and_month():
    assert parse_datetime("2.1.2026 8:5", "Europe/Moscow", NOW) == datetime(
        2026, 1, 2, 5, 5, tzinfo=UTC
    )


@pytest.mark.parametrize("text", ["31.02.2026", "31.02.2026 8:00", "2026-02-31T8:00"])
def test_impossible_combined_or_date_only_input_keeps_calendar_error(text):
    with pytest.raises(ValueError, match="Такой даты не существует"):
        parse_datetime(text, "Europe/Moscow", NOW)


@pytest.mark.parametrize("text", ["24.10.2026 24:00", "24.10.2026 8:60", "24.10.2026 24"])
def test_combined_input_keeps_clock_range_error(text):
    with pytest.raises(ValueError, match="Некорректное время"):
        parse_datetime(text, "Europe/Moscow", NOW)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("сегодня 8", datetime(2026, 1, 1, 5, tzinfo=UTC)),
        ("Сегодня в 8:5", datetime(2026, 1, 1, 5, 5, tzinfo=UTC)),
        ("ЗАВТРА 8.00", datetime(2026, 1, 2, 5, tzinfo=UTC)),
        ("Послезавтра В 8 : 05", datetime(2026, 1, 3, 5, 5, tzinfo=UTC)),
        ("завтра\nв\u00a08:00", datetime(2026, 1, 2, 5, tzinfo=UTC)),
    ],
)
def test_relative_date_and_clock(text, expected):
    assert parse_datetime(text, "Europe/Moscow", NOW) == expected


@pytest.mark.parametrize(
    ("text", "timezone", "expected"),
    [
        ("сегодня", "Europe/Moscow", date(2026, 10, 24)),
        ("ЗАВТРА", "Europe/Moscow", date(2026, 10, 25)),
        ("послезавтра", "Europe/Moscow", date(2026, 10, 26)),
        ("сегодня", "America/New_York", date(2026, 10, 23)),
        ("завтра", "America/New_York", date(2026, 10, 24)),
        ("Послезавтра", "America/New_York", date(2026, 10, 25)),
        ("2/1/2026", "Europe/Moscow", date(2026, 1, 2)),
    ],
)
def test_user_date_uses_local_day_and_also_accepts_explicit_dates(text, timezone, expected):
    now = datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
    assert parse_user_date(text, timezone, now) == expected


def test_relative_combined_date_uses_local_day_at_utc_midnight_boundary():
    now = datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
    assert parse_datetime("сегодня в 8", "Europe/Moscow", now) == datetime(
        2026, 10, 24, 5, tzinfo=UTC
    )
    assert parse_datetime("завтра 8", "America/New_York", now) == datetime(
        2026, 10, 24, 12, tzinfo=UTC
    )


@pytest.mark.parametrize("text", ["сегодня 8:00", "сегодня 9", "1/1/2026 8", "2026-01-01T09:00"])
def test_flexible_and_relative_past_times_are_still_rejected(text):
    with pytest.raises(ValueError, match="прошедшее"):
        parse_datetime(text, "Europe/Moscow", datetime(2026, 1, 1, 6, tzinfo=UTC))


def test_relative_date_year_boundary():
    now = datetime(2026, 12, 31, 20, tzinfo=UTC)
    assert parse_user_date("завтра", "Europe/Moscow", now) == date(2027, 1, 1)
    assert parse_user_date("послезавтра", "Europe/Moscow", now) == date(2027, 1, 2)


def test_relative_dates_share_one_default_reference(monkeypatch):
    references = [
        datetime(2026, 10, 23, 20, 58, 30, tzinfo=UTC),
        datetime(2026, 10, 23, 21, tzinfo=UTC),
    ]
    monkeypatch.setattr("bot.utils.datetime_utils.utc_now", lambda: references.pop(0))
    assert parse_datetime("сегодня 23:59", "Europe/Moscow") == datetime(
        2026, 10, 23, 20, 59, tzinfo=UTC
    )
    assert len(references) == 1


@pytest.mark.parametrize(
    "operation",
    [
        lambda: parse_date(" " * 33 + "2.1.2026"),
        lambda: parse_time("8" + " " * 33),
        lambda: parse_user_date("сегодня" + " " * 33, "UTC", NOW),
        lambda: parse_datetime("2.1.2026" + " " * 129 + "8:00", "UTC", NOW),
    ],
)
def test_user_input_length_is_bounded(operation):
    with pytest.raises(ValueError):
        operation()


@pytest.mark.parametrize("text", ["tomorrow", "в пятницу", "через день", "сегодня в 8:00", None])
def test_user_date_rejects_unrecognized_phrases_and_clock_parts(text):
    with pytest.raises(ValueError):
        parse_user_date(text, "Europe/Moscow", NOW)


def test_relative_date_overflow_is_a_validation_error():
    with pytest.raises(ValueError):
        parse_user_date("завтра", "UTC", datetime(9999, 12, 31, tzinfo=UTC))


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        ("Europe/Moscow", datetime(2026, 1, 2, 6, tzinfo=UTC)),
        ("Asia/Tokyo", datetime(2026, 1, 2, 0, tzinfo=UTC)),
        ("America/New_York", datetime(2026, 1, 2, 14, tzinfo=UTC)),
        ("Asia/Kathmandu", datetime(2026, 1, 2, 3, 15, tzinfo=UTC)),
    ],
)
def test_parse_datetime_converts_user_timezone_to_utc(timezone, expected):
    assert parse_datetime("02.01.2026 09:00", timezone, NOW) == expected


@pytest.mark.parametrize("text", ["01.01.2026 02:59", "01.01.2026 03:00"])
def test_past_and_exact_present_are_rejected(text):
    with pytest.raises(ValueError, match="прошедшее"):
        parse_datetime(text, "Europe/Moscow", NOW)


def test_manual_dst_gap_is_rejected():
    with pytest.raises(ValueError, match="не существует"):
        parse_datetime("29.03.2026 02:30", "Europe/Berlin", NOW)


@pytest.mark.parametrize("text", ["29/3/2026 2:30", "2026-03-29T2.30", "завтра в 2:30"])
def test_flexible_and_relative_times_preserve_dst_gap_error(text):
    now = datetime(2026, 3, 28, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="не существует.*перевода часов"):
        parse_datetime(text, "Europe/Berlin", now)


def test_extreme_manual_date_reports_validation_error_instead_of_overflow():
    with pytest.raises(ValueError):
        parse_datetime("01.01.0001 00:00", "Europe/Moscow", NOW)


def test_ambiguous_time_uses_first_occurrence():
    result = parse_datetime("25.10.2026 02:30", "Europe/Berlin", NOW)
    assert result == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_ambiguous_second_fold_is_not_silently_selected_if_first_is_past():
    with pytest.raises(ValueError, match="прошедшее"):
        parse_datetime(
            "25.10.2026 02:30", "Europe/Berlin", datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
        )


@pytest.mark.parametrize("name", ["Not/AZone", "", "../UTC", "/UTC", None])
def test_invalid_timezone(name):
    with pytest.raises(ValueError):
        validate_timezone(name)


def test_timezone_is_trimmed():
    assert validate_timezone(" Europe/Moscow ") == "Europe/Moscow"


def test_day_bounds_follow_local_day():
    now = datetime(2026, 10, 23, 22, tzinfo=UTC)
    assert local_day_bounds("Europe/Moscow", now) == (
        datetime(2026, 10, 23, 21, tzinfo=UTC),
        datetime(2026, 10, 24, 21, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("now", "hours"),
    [
        (datetime(2026, 3, 29, 12, tzinfo=UTC), 23),
        (datetime(2026, 10, 25, 12, tzinfo=UTC), 25),
    ],
)
def test_dst_day_can_be_23_or_25_hours(now, hours):
    start, end = local_day_bounds("Europe/Berlin", now)
    assert end - start == timedelta(hours=hours)


def test_midnight_dst_gap_bounds_start_at_first_existing_time():
    start, end = local_day_bounds("America/Santiago", datetime(2026, 9, 6, 12, tzinfo=UTC))
    assert start == datetime(2026, 9, 6, 4, tzinfo=UTC)
    assert end - start == timedelta(hours=23)


@pytest.mark.parametrize("repeat", ["daily", "weekdays", "weekly"])
def test_recurrence_skips_missed_periods(repeat):
    scheduled = datetime(2000, 1, 3, 6, tzinfo=UTC)  # Monday at 09:00 Moscow.
    after = datetime(2026, 10, 23, 7, tzinfo=UTC)  # Friday after reminder.
    expected = {
        "daily": datetime(2026, 10, 24, 6, tzinfo=UTC),
        "weekdays": datetime(2026, 10, 26, 6, tzinfo=UTC),
        "weekly": datetime(2026, 10, 26, 6, tzinfo=UTC),
    }
    assert next_occurrence(scheduled, repeat, "Europe/Moscow", after) == expected[repeat]


def test_daily_recurrence_preserves_local_hour_across_dst():
    scheduled = datetime(2026, 3, 28, 8, tzinfo=UTC)  # 09:00 in Berlin.
    assert next_occurrence(scheduled, "daily", "Europe/Berlin", scheduled) == datetime(
        2026, 3, 29, 7, tzinfo=UTC
    )


def test_daily_recurrence_skips_nonexistent_clock_without_losing_anchor():
    scheduled = datetime(2026, 3, 28, 1, 30, tzinfo=UTC)
    next_at = next_occurrence(scheduled, "daily", "Europe/Berlin", scheduled)
    assert next_at == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)
    assert next_occurrence(next_at, "daily", "Europe/Berlin", next_at) == datetime(
        2026, 3, 31, 0, 30, tzinfo=UTC
    )


def test_weekly_recurrence_skips_dst_gap_and_keeps_weekday():
    scheduled = datetime(2026, 3, 22, 1, 30, tzinfo=UTC)
    assert next_occurrence(scheduled, "weekly", "Europe/Berlin", scheduled) == datetime(
        2026, 4, 5, 0, 30, tzinfo=UTC
    )


def test_weekday_recurrence_skips_weekend_and_preserves_time_after_dst():
    scheduled = datetime(2026, 3, 27, 8, tzinfo=UTC)
    assert next_occurrence(scheduled, "weekdays", "Europe/Berlin", scheduled) == datetime(
        2026, 3, 30, 7, tzinfo=UTC
    )


def test_recurrence_is_strictly_after_reference():
    scheduled = datetime(2026, 1, 1, 6, tzinfo=UTC)
    after = datetime(2026, 1, 2, 6, tzinfo=UTC)
    assert next_occurrence(scheduled, "daily", "Europe/Moscow", after) == datetime(
        2026, 1, 3, 6, tzinfo=UTC
    )


def test_recurrence_does_not_return_initial_occurrence():
    scheduled = datetime(2026, 1, 2, 6, tzinfo=UTC)
    assert next_occurrence(scheduled, "daily", "Europe/Moscow", NOW) == datetime(
        2026, 1, 3, 6, tzinfo=UTC
    )


def test_recurrence_accepts_string_enum():
    class Repeat(str, Enum):
        DAILY = "daily"

    scheduled = datetime(2026, 1, 1, 6, tzinfo=UTC)
    assert next_occurrence(scheduled, Repeat.DAILY, "UTC", scheduled).day == 2


def test_unknown_recurrence_is_rejected():
    with pytest.raises(ValueError, match="повторения"):
        next_occurrence(NOW, "monthly", "UTC", NOW)


def test_format_datetime_uses_local_zone():
    assert format_datetime(datetime(2026, 10, 23, 23, 5, tzinfo=UTC), "Europe/Moscow") == "24.10.2026 02:05"


@pytest.mark.parametrize(
    "operation",
    [
        lambda: parse_datetime("02.01.2026 09:00", "UTC", datetime(2026, 1, 1)),
        lambda: combine_local(date(2026, 1, 2), time(9), "UTC", datetime(2026, 1, 1)),
        lambda: local_day_bounds("UTC", datetime(2026, 1, 1)),
        lambda: next_occurrence(datetime(2026, 1, 1), "daily", "UTC", NOW),
        lambda: next_occurrence(NOW, "daily", "UTC", datetime(2026, 1, 1)),
        lambda: format_datetime(datetime(2026, 1, 1), "UTC"),
        lambda: parse_user_date("сегодня", "UTC", datetime(2026, 1, 1)),
    ],
)
def test_naive_timestamps_are_rejected(operation):
    with pytest.raises(ValueError, match="часовой пояс"):
        operation()
