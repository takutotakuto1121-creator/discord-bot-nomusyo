"""Recurrence expansion (V2-50): rules, DST, bounds, rejection."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nomusyo.schedule import recurrence
from nomusyo.schedule.errors import ValidationError
from nomusyo.schedule.models import Window
from nomusyo.schedule.recurrence import (
    iter_starts,
    occurrence_starts,
    series_until_utc,
    validate_rrule,
)

JST = ZoneInfo("Asia/Tokyo")
NY = ZoneInfo("America/New_York")


def jst(*a):
    return datetime(*a, tzinfo=JST)


def ny(*a):
    return datetime(*a, tzinfo=NY)


def win(start, end):
    return Window(start, end)


def locs(pairs):
    return [p[0] for p in pairs]


def days_of(pairs):
    return [(p[1].month, p[1].day) for p in pairs]


# ---- basic expansion -----------------------------------------------------------------------
def test_daily_basic():
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 9), win(jst(2026, 1, 1), jst(2026, 1, 4)))
    assert locs(r) == [
        "2026-01-01T09:00:00",
        "2026-01-02T09:00:00",
        "2026-01-03T09:00:00",
    ]
    assert all(s.tzinfo is not None and s.utcoffset() == timedelta(hours=9) for _, s in r)


def test_weekly_mo_we():
    # 2026-01-05 is Monday
    r = occurrence_starts(
        "FREQ=WEEKLY;BYDAY=MO,WE", jst(2026, 1, 5, 9), win(jst(2026, 1, 5), jst(2026, 1, 19))
    )
    assert days_of(r) == [(1, 5), (1, 7), (1, 12), (1, 14)]


def test_weekdays():
    r = occurrence_starts(
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        jst(2026, 1, 5, 9),
        win(jst(2026, 1, 5), jst(2026, 1, 12)),
    )
    assert days_of(r) == [(1, d) for d in (5, 6, 7, 8, 9)]


def test_second_tuesday():
    r = occurrence_starts(
        "FREQ=MONTHLY;BYDAY=2TU", jst(2026, 1, 1, 9), win(jst(2026, 1, 1), jst(2026, 4, 1))
    )
    assert days_of(r) == [(1, 13), (2, 10), (3, 10)]


def test_year_boundary_weekdays():
    r = occurrence_starts(
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        jst(2025, 12, 30, 9),
        win(jst(2025, 12, 30), jst(2026, 1, 9)),
    )
    assert [(s.year, s.month, s.day) for _, s in r] == [
        (2025, 12, 30),
        (2025, 12, 31),
        (2026, 1, 1),
        (2026, 1, 2),
        (2026, 1, 5),
        (2026, 1, 6),
        (2026, 1, 7),
        (2026, 1, 8),
    ]


# ---- month end / leap ----------------------------------------------------------------------
def test_bymonthday_31_skips_short_months():
    r = occurrence_starts(
        "FREQ=MONTHLY;BYMONTHDAY=31", jst(2026, 1, 31, 9), win(jst(2026, 1, 1), jst(2026, 6, 1))
    )
    assert days_of(r) == [(1, 31), (3, 31), (5, 31)]


def test_bymonthday_minus1_last_day():
    r = occurrence_starts(
        "FREQ=MONTHLY;BYMONTHDAY=-1", jst(2028, 1, 31, 9), win(jst(2028, 1, 1), jst(2028, 5, 1))
    )
    assert days_of(r) == [(1, 31), (2, 29), (3, 31), (4, 30)]


def test_yearly_leap_day_every_four_years():
    r = occurrence_starts("FREQ=YEARLY", jst(2024, 2, 29, 9), win(jst(2024, 1, 1), jst(2037, 1, 1)))
    assert [s.year for _, s in r] == [2024, 2028, 2032, 2036]


# ---- DST -----------------------------------------------------------------------------------
def test_dst_gap_shifts_start_but_keeps_local_label():
    r = occurrence_starts("FREQ=DAILY", ny(2024, 3, 8, 2, 30), win(ny(2024, 3, 9), ny(2024, 3, 12)))
    by_local = dict(r)
    gap = by_local["2024-03-10T02:30:00"]
    assert (gap.hour, gap.minute) == (3, 30)
    assert by_local["2024-03-09T02:30:00"].hour == 2
    assert by_local["2024-03-11T02:30:00"].hour == 2


def test_dst_wall_clock_kept_across_offset_change():
    r = occurrence_starts("FREQ=DAILY", ny(2024, 3, 8, 9, 0), win(ny(2024, 3, 8), ny(2024, 3, 13)))
    assert all(s.hour == 9 for _, s in r)
    offsets = {s.utcoffset() for _, s in r}
    assert offsets == {timedelta(hours=-5), timedelta(hours=-4)}


def test_dst_overlap_day_single_occurrence():
    r = occurrence_starts(
        "FREQ=DAILY", ny(2024, 11, 1, 1, 30), win(ny(2024, 11, 1), ny(2024, 11, 6))
    )
    assert len(r) == 5
    assert all((s.hour, s.minute) == (1, 30) for _, s in r)
    by_local = dict(r)
    assert by_local["2024-11-03T01:30:00"].utcoffset() in (timedelta(hours=-4), timedelta(hours=-5))
    assert by_local["2024-11-04T01:30:00"].utcoffset() == timedelta(hours=-5)


# ---- COUNT / UNTIL -------------------------------------------------------------------------
def test_count_limits():
    r = occurrence_starts(
        "FREQ=DAILY;COUNT=3", jst(2026, 1, 1, 9), win(jst(2026, 1, 1), jst(2026, 2, 1))
    )
    assert len(r) == 3


def test_until_converted_from_utc_to_wall_clock_and_inclusive():
    # 2026-01-03T00:00Z == 2026-01-03 09:00 JST -> last occurrence is included
    r = occurrence_starts(
        "FREQ=DAILY;UNTIL=20260103T000000Z",
        jst(2026, 1, 1, 9),
        win(jst(2026, 1, 1), jst(2026, 2, 1)),
    )
    assert days_of(r) == [(1, 1), (1, 2), (1, 3)]


def test_until_one_second_before_excludes():
    r = occurrence_starts(
        "FREQ=DAILY;UNTIL=20260102T235959Z",
        jst(2026, 1, 1, 9),
        win(jst(2026, 1, 1), jst(2026, 2, 1)),
    )
    # 2026-01-02T23:59:59Z is Jan 3 08:59:59 JST: Jan 3 09:00 excluded
    assert days_of(r) == [(1, 1), (1, 2)]


def test_count_and_until_rejected():
    with pytest.raises(ValidationError):
        validate_rrule("FREQ=DAILY;COUNT=3;UNTIL=20260103T000000Z", jst(2026, 1, 1, 9))


def test_series_until_finite_count():
    end = series_until_utc("FREQ=DAILY;COUNT=3", jst(2026, 1, 1, 9), timedelta(hours=1))
    assert end == datetime(2026, 1, 3, 1, 0, tzinfo=UTC)


def test_series_until_with_until():
    end = series_until_utc("FREQ=DAILY;UNTIL=20260103T000000Z", jst(2026, 1, 1, 9))
    assert end == datetime(2026, 1, 3, 0, 0, tzinfo=UTC)


def test_series_until_unbounded_is_none():
    assert series_until_utc("FREQ=DAILY", jst(2026, 1, 1, 9)) is None


def test_series_until_empty_rule_falls_back_to_start_plus_span():
    # UNTIL before dtstart: no occurrences
    end = series_until_utc(
        "FREQ=DAILY;UNTIL=20250101T000000Z", jst(2026, 1, 1, 9), timedelta(hours=2)
    )
    assert end == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)


# ---- windows -------------------------------------------------------------------------------
def test_span_includes_occurrence_started_before_window():
    # daily 22:00 for 3h: the Jan 1 occurrence is still running at Jan 2 00:30
    w = win(jst(2026, 1, 2, 0, 30), jst(2026, 1, 2, 1, 0))
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 22), w, span=timedelta(hours=3))
    assert locs(r) == ["2026-01-01T22:00:00"]


def test_span_end_exactly_at_window_start_excluded():
    w = win(jst(2026, 1, 2, 1, 0), jst(2026, 1, 2, 2, 0))
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 22), w, span=timedelta(hours=3))
    assert r == []


def test_window_end_exclusive():
    w = win(jst(2026, 1, 1), jst(2026, 1, 2, 9))
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 9), w)
    assert locs(r) == ["2026-01-01T09:00:00"]
    w2 = win(jst(2026, 1, 1), jst(2026, 1, 2, 9, 0, 1))
    assert len(occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 9), w2)) == 2


def test_instant_event_window_start_inclusive():
    w = win(jst(2026, 1, 2, 9), jst(2026, 1, 3))
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 9), w)
    assert locs(r) == ["2026-01-02T09:00:00"]


def test_all_day_daily_with_day_span():
    w = win(jst(2026, 1, 3), jst(2026, 1, 4))
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1), w, span=timedelta(days=1))
    assert locs(r) == ["2026-01-03T00:00:00"]


# ---- limits --------------------------------------------------------------------------------
def test_max_results_caps(monkeypatch):
    monkeypatch.setattr(recurrence, "MAX_RESULTS", 5)
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 9), win(jst(2026, 1, 1), jst(2027, 1, 1)))
    assert len(r) == 5


def test_max_scan_stops_runaway(monkeypatch):
    monkeypatch.setattr(recurrence, "MAX_SCAN", 10)
    r = occurrence_starts("FREQ=DAILY", jst(2026, 1, 1, 9), win(jst(2026, 6, 1), jst(2026, 7, 1)))
    assert r == []
    assert len(list(iter_starts("FREQ=DAILY", jst(2026, 1, 1, 9)))) == 10


def test_empty_rule_returns_nothing_fast():
    import time

    t = time.monotonic()
    r = occurrence_starts(
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",
        jst(2026, 1, 1, 9),
        win(jst(2026, 1, 1), jst(2030, 1, 1)),
    )
    assert r == []
    assert time.monotonic() - t < 5


# ---- validation ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;BYMONTH=" + "1," * 100,
        "FREQ=DAILY;" + "X" * 200,
        "FREQ=SECONDLY",
        "FREQ=HOURLY",
        "FREQ=DAILY;BYHOUR=9",
        "FREQ=DAILY;DTSTART=20260101T000000",
        "FREQ=DAILY;FOO=1",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;INTERVAL=100",
        "FREQ=DAILY;COUNT=99999",
        "FREQ=DAILY;COUNT=0",
        "FREQ=DAILY;UNTIL=20260103",
        "FREQ=DAILY;UNTIL=20261301T000000Z",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=DAILY\nCOUNT=3",
        "FREQ=DAILY;\nCOUNT=3",
        "FREQ=DAILY;FREQ=WEEKLY",
        "FREQ=DAILY;COUNT=3;COUNT=4",
        "FREQ=DAILY;BYDAY=月",
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "FREQ=MONTHLY;BYMONTHDAY=32",
        "FREQ=YEARLY;BYMONTH=13",
        "",
        "   ",
        "COUNT=3",
        "FREQ=DAILY;",
    ],
)
def test_rejected(rule):
    with pytest.raises(ValidationError):
        validate_rrule(rule, jst(2026, 1, 1, 9))


def _rule_of_length(n):
    head = "FREQ=MONTHLY;BYMONTHDAY="
    items = ["+1"] * 31
    pos = ["+1"] * 10
    tail = ";BYSETPOS="

    def build():
        return head + ",".join(items) + tail + ",".join(pos)

    # widen one item at a time ("+1" -> "+01" -> "+001") until the target length is hit
    for width in ("+01", "+001"):
        for lst in (items, pos):
            for i in range(len(lst)):
                if len(build()) >= n:
                    break
                lst[i] = width
    rule = build()
    assert len(rule) == n, len(rule)
    return rule


def test_length_200_accepted_201_rejected():
    validate_rrule(_rule_of_length(200), jst(2026, 1, 1, 9))
    with pytest.raises(ValidationError):
        validate_rrule(_rule_of_length(201), jst(2026, 1, 1, 9))


def test_lowercase_normalized():
    out = validate_rrule("freq=weekly;byday=mo,we", jst(2026, 1, 1, 9))
    assert out == "FREQ=WEEKLY;BYDAY=MO,WE"


def test_prefix_stripped():
    assert validate_rrule("RRULE:FREQ=DAILY", jst(2026, 1, 1, 9)) == "FREQ=DAILY"
    assert validate_rrule("rrule:freq=daily", jst(2026, 1, 1, 9)) == "FREQ=DAILY"


def test_naive_or_nonzoneinfo_start_rejected():
    with pytest.raises(ValidationError):
        validate_rrule("FREQ=DAILY", datetime(2026, 1, 1, 9))
    with pytest.raises(ValidationError):
        validate_rrule("FREQ=DAILY", datetime(2026, 1, 1, 9, tzinfo=UTC))


def test_non_string_rejected():
    with pytest.raises(ValidationError):
        validate_rrule(None, jst(2026, 1, 1, 9))  # type: ignore[arg-type]
