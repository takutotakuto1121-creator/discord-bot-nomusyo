"""Strict-format parsers for schedule input. No natural language (that is phase 4).

Supported ``when`` formats (full-width digits/colon are normalized with NFKC):

    YYYY-MM-DD HH:MM      2026-11-03 15:00      (also YYYY/MM/DD)
    YYYY-MM-DD            2026-11-03            (all-day)
    M/D HH:MM             11/3 15:00            (year completed, see below)
    M/D                   11/3                  (all-day)
    今日|明日|明後日 [HH:MM]   明日 9:00           (without time: all-day)

Year completion for ``M/D``: current year; if the date is before today (local), next year
(skipping to the next valid year for 2/29). With ``allow_past=True`` no rollover happens.
Past rejection: a timed value before ``now`` or an all-day value before today raises
``PastDateError`` unless ``allow_past=True``.

Duration: ``90m``, ``2h``, ``1h30m``, ``1d`` (1..31 days). Reminders: comma separated
``10m``, ``1h``, ``1d`` (= 1440 min before) or ``1d@20:00`` (N days before at that time;
``0d@20:00`` = same day). ``@`` is only valid with ``d``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nomusyo.schedule.errors import ParseError, PastDateError
from nomusyo.schedule.models import (
    MAX_DAY_OFFSET,
    MAX_REMINDERS,
    ReminderSpec,
    local_midnight,
    require_aware,
)

_TIME = r"(?P<h>\d{1,2}):(?P<mi>\d{2})"
_FULL = re.compile(rf"^(?P<y>\d{{4}})[-/](?P<m>\d{{1,2}})[-/](?P<d>\d{{1,2}})(?:\s+{_TIME})?$")
_MD = re.compile(rf"^(?P<m>\d{{1,2}})/(?P<d>\d{{1,2}})(?:\s+{_TIME})?$")
_REL = re.compile(rf"^(?P<rel>今日|明日|明後日)(?:\s+{_TIME})?$")
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")
_REMIND = re.compile(r"^(\d+)([mhd])(?:@(\d{1,2}:\d{2}))?$")
_REL_DAYS = {"今日": 0, "明日": 1, "明後日": 2}
MAX_DURATION_MIN = 31 * 24 * 60


@dataclass(frozen=True)
class ParsedWhen:
    start: datetime  # aware; all-day = local midnight
    all_day: bool


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def parse_hhmm(text: str) -> time:
    """Parse ``HH:MM`` (24h). Raises ``ParseError`` (message never echoes the input)."""
    m = _HHMM.match(_norm(text))
    if not m:
        raise ParseError("時刻は HH:MM 形式で指定してください")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise ParseError("時刻の範囲が不正です")
    return time(h, mi)


def _time_of(m: re.Match[str]) -> time | None:
    if m.group("h") is None:
        return None
    h, mi = int(m.group("h")), int(m.group("mi"))
    if h > 23 or mi > 59:
        raise ParseError("時刻の範囲が不正です")
    return time(h, mi)


def _make_date(y: int, mo: int, d: int) -> date:
    try:
        return date(y, mo, d)
    except ValueError:
        raise ParseError("存在しない日付です") from None


def parse_when(text: str, *, now: datetime, tz: ZoneInfo, allow_past: bool = False) -> ParsedWhen:
    require_aware(now, "現在時刻")
    s = _norm(text)
    if not s:
        raise ParseError("日時を指定してください")
    today = now.astimezone(tz).date()

    if m := _FULL.match(s):
        d = _make_date(int(m["y"]), int(m["m"]), int(m["d"]))
    elif m := _REL.match(s):
        d = today + timedelta(days=_REL_DAYS[m["rel"]])
    elif m := _MD.match(s):
        mo, dd = int(m["m"]), int(m["d"])
        d = _complete_year(mo, dd, today, allow_past)
    else:
        raise ParseError(
            "日時の形式が不正です(例: 2026-11-03 15:00 / 11/3 15:00 / 明日 9:00 / 今日)"
        )

    t = _time_of(m)
    if t is None:
        start, all_day = local_midnight(d, tz), True
        if not allow_past and d < today:
            raise PastDateError("過去の日付は指定できません")
    else:
        start, all_day = datetime.combine(d, t, tzinfo=tz), False
        if not allow_past and start < now:
            raise PastDateError("過去の日時は指定できません")
    return ParsedWhen(start=start, all_day=all_day)


def _complete_year(month: int, day: int, today: date, allow_past: bool) -> date:
    _make_date(2024, month, day)  # validates month/day against a leap year
    year = today.year
    for y in range(year, year + 9):
        try:
            d = date(y, month, day)
        except ValueError:
            continue  # 2/29 in a non-leap year
        if allow_past or d >= today:
            return d
    raise ParseError("存在しない日付です")


def parse_duration(text: str) -> int:
    """Return minutes. Raises ``ParseError``."""
    s = _norm(text).lower()
    m = _DURATION.match(s)
    if not s or not m or not any(m.groups()):
        raise ParseError("所要時間は 90m / 2h / 1h30m / 1d の形式で指定してください")
    d, h, mi = (int(g) if g else 0 for g in m.groups())
    total = d * 1440 + h * 60 + mi
    if total <= 0 or total > MAX_DURATION_MIN:
        raise ParseError("所要時間の範囲が不正です(1分〜31日)")
    return total


def parse_reminders(text: str) -> tuple[ReminderSpec, ...]:
    """Parse ``10m,1h,1d@20:00``. Empty text gives an empty tuple. Duplicates are merged."""
    s = _norm(text)
    if not s:
        return ()
    specs: list[ReminderSpec] = []
    for part in s.split(","):
        part = part.strip().lower()
        m = _REMIND.match(part)
        if not m:
            raise ParseError("リマインドは 10m / 1h / 1d / 1d@20:00 の形式で指定してください")
        n, unit, at = int(m[1]), m[2], m[3]
        if at is not None:
            if unit != "d":
                raise ParseError("@時刻は日単位(例 1d@20:00)でのみ指定できます")
            if n > MAX_DAY_OFFSET:
                raise ParseError(f"日数は{MAX_DAY_OFFSET}日前までです")
            t = parse_hhmm(at)
            spec = ReminderSpec(at_local=f"{t.hour:02d}:{t.minute:02d}", day_offset=n)
        else:
            minutes = n * {"m": 1, "h": 60, "d": 1440}[unit]
            if minutes > MAX_DURATION_MIN:
                raise ParseError("リマインドの範囲が不正です")
            spec = ReminderSpec(offset_min=minutes)
        if spec not in specs:
            specs.append(spec)
    if len(specs) > MAX_REMINDERS:
        raise ParseError(f"リマインドは{MAX_REMINDERS}件までです")
    return tuple(specs)
