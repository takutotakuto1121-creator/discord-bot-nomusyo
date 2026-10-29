"""Recurrence expansion (RRULE) for events. Discord independent.

Design notes (schedule-data-model.md section 2):

* A recurring event is ``dtstart_local`` + ``tz`` + an RRULE string. The rule is evaluated on
  **naive wall-clock** datetimes and each result is attached to ``tz`` afterwards, so a
  "09:00 every day" event stays at 09:00 across DST changes.
* ``UNTIL`` is stored in UTC (``YYYYMMDDTHHMMSSZ``, mandatory form); it is converted to the
  event's wall clock before evaluation. ``COUNT`` and ``UNTIL`` are mutually exclusive.
* The RRULE string is untrusted input: strict allowlist of FREQ / parameters / value shapes
  and a length cap. Anything else raises ``ValidationError``.
* Expansion is bounded (scan and result caps) so a pathological rule cannot run away.
* Months lacking the day (e.g. BYMONTHDAY=31 in April) are skipped (dateutil semantics).
  Use ``BYMONTHDAY=-1`` for "last day of month".
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from nomusyo.schedule.errors import ValidationError
from nomusyo.schedule.models import Window, require_aware

MAX_RRULE_LEN = 200
MAX_INTERVAL = 99
MAX_COUNT = 1000
MAX_RESULTS = 500  # occurrences returned per event per window
MAX_SCAN = 50_000  # candidates examined per expansion (runaway guard)

ALLOWED_FREQ = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_UNTIL_RE = re.compile(r"\d{8}T\d{6}Z")
_BYDAY_RE = re.compile(r"(?:[+-]?(?:[1-9]|[1-4]\d|5[0-3]))?(?:MO|TU|WE|TH|FR|SA|SU)")
_INT_RE = re.compile(r"[+-]?\d{1,3}")
_ALLOWED_KEYS = frozenset(
    {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"}
)

_BAD = "繰り返しの指定が不正です"


def _int_list(value: str, lo: int, hi: int, *, allow_zero: bool = False, max_items: int = 31):
    items = value.split(",")
    if not value or len(items) > max_items:
        raise ValidationError(_BAD)
    for it in items:
        if not _INT_RE.fullmatch(it):
            raise ValidationError(_BAD)
        n = int(it)
        if not lo <= n <= hi or (n == 0 and not allow_zero):
            raise ValidationError(_BAD)


def _parse_rule(rrule: str) -> dict[str, str]:
    """Validate ``rrule`` and return its parameters (upper-cased keys). Raises on anything
    outside the allowlist."""
    if not isinstance(rrule, str):
        raise ValidationError(_BAD)
    text = rrule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    if not text or len(text) > MAX_RRULE_LEN or not text.isascii() or not text.isprintable():
        raise ValidationError(_BAD)
    params: dict[str, str] = {}
    for part in text.split(";"):
        key, sep, value = part.partition("=")
        key = key.strip().upper()
        value = value.strip().upper()
        if not sep or key not in _ALLOWED_KEYS or key in params or not value:
            raise ValidationError(_BAD)
        params[key] = value
    if params.get("FREQ") not in ALLOWED_FREQ:
        raise ValidationError(_BAD)
    if "COUNT" in params and "UNTIL" in params:
        raise ValidationError(_BAD)
    for key, upper in (("INTERVAL", MAX_INTERVAL), ("COUNT", MAX_COUNT)):
        if key in params:
            if not params[key].isdigit() or not 1 <= int(params[key]) <= upper:
                raise ValidationError(_BAD)
    if "UNTIL" in params:
        if not _UNTIL_RE.fullmatch(params["UNTIL"]):
            raise ValidationError(_BAD)
        try:
            datetime.strptime(params["UNTIL"], "%Y%m%dT%H%M%SZ")
        except ValueError:
            raise ValidationError(_BAD) from None
    if "BYDAY" in params:
        days = params["BYDAY"].split(",")
        if len(days) > 14 or not all(_BYDAY_RE.fullmatch(d) for d in days):
            raise ValidationError(_BAD)
    if "BYMONTHDAY" in params:
        _int_list(params["BYMONTHDAY"], -31, 31)
    if "BYMONTH" in params:
        _int_list(params["BYMONTH"], 1, 12, max_items=12)
    if "BYSETPOS" in params:
        _int_list(params["BYSETPOS"], -366, 366, max_items=10)
    if "WKST" in params and params["WKST"] not in _WEEKDAYS:
        raise ValidationError(_BAD)
    return params


def _build(rrule: str, dtstart_local: datetime, zone: ZoneInfo):
    """dateutil rule over naive wall-clock datetimes (UNTIL converted to wall clock)."""
    params = _parse_rule(rrule)
    if "UNTIL" in params:
        until = datetime.strptime(params["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        params["UNTIL"] = until.astimezone(zone).strftime("%Y%m%dT%H%M%S")
    text = ";".join(f"{k}={v}" for k, v in params.items())
    try:
        return rrulestr(text, dtstart=dtstart_local)
    except (ValueError, TypeError, OverflowError):
        raise ValidationError(_BAD) from None


def _naive_start(dtstart: datetime) -> tuple[datetime, ZoneInfo]:
    require_aware(dtstart, "開始日時")
    zone = dtstart.tzinfo
    if not isinstance(zone, ZoneInfo):
        raise ValidationError("タイムゾーンが不正です")
    return dtstart.replace(tzinfo=None), zone


def validate_rrule(rrule: str, dtstart: datetime) -> str:
    """Return the normalized RRULE (no ``RRULE:`` prefix, upper-case) or raise
    ``ValidationError``. ``dtstart`` must be aware in a ``ZoneInfo`` zone."""
    naive, zone = _naive_start(dtstart)
    _build(rrule, naive, zone)
    params = _parse_rule(rrule)
    return ";".join(f"{k}={v}" for k, v in params.items())


def iter_starts(rrule: str, dtstart: datetime) -> Iterator[datetime]:
    """Occurrence starts in order as aware datetimes (wall clock kept; a wall time that does
    not exist on a DST-gap day is shifted forward by the zone rules). Bounded by ``MAX_SCAN``."""
    naive, zone = _naive_start(dtstart)
    rule = _build(rrule, naive, zone)
    for i, local in enumerate(rule):
        if i >= MAX_SCAN:
            return
        yield _attach(local, zone)


def _attach(local: datetime, zone: ZoneInfo) -> datetime:
    return local.replace(tzinfo=zone).astimezone(UTC).astimezone(zone)


def occurrence_starts(
    rrule: str, dtstart: datetime, window: Window, *, span: timedelta = timedelta(0)
) -> list[tuple[str, datetime]]:
    """Occurrences whose start lies in ``[window.start - span, window.end)``.

    ``span`` is the occurrence length, so occurrences that began before the window but still
    overlap it are included. Returns ``(occurrence_local, aware_start)`` pairs where
    ``occurrence_local`` is the rule's own wall-clock string (``YYYY-MM-DDTHH:MM:SS``).
    At most ``MAX_RESULTS`` items; scanning stops after ``MAX_SCAN`` candidates."""
    naive, zone = _naive_start(dtstart)
    rule = _build(rrule, naive, zone)
    lo = window.start.astimezone(zone).replace(tzinfo=None) - span - timedelta(days=1)
    hi = window.end.astimezone(zone).replace(tzinfo=None) + timedelta(days=1)
    out: list[tuple[str, datetime]] = []
    for i, local in enumerate(rule):
        if i >= MAX_SCAN or local >= hi:
            break
        if local < lo:
            continue
        start = _attach(local, zone)
        if start >= window.end:
            break
        if (start + span <= window.start) if span > timedelta(0) else (start < window.start):
            continue
        out.append((local.strftime("%Y-%m-%dT%H:%M:%S"), start))
        if len(out) >= MAX_RESULTS:
            break
    return out


def series_until_utc(
    rrule: str, dtstart: datetime, span: timedelta = timedelta(0)
) -> datetime | None:
    """End (UTC) of the last occurrence for COUNT/UNTIL rules, ``None`` if unbounded.
    Intended for the ``events.until_utc`` column (SQL pre-filter in ``expand``)."""
    params = _parse_rule(rrule)
    if "COUNT" not in params and "UNTIL" not in params:
        return None
    last: datetime | None = None
    for s in iter_starts(rrule, dtstart):
        last = s
    if last is None:
        return dtstart.astimezone(UTC) + span
    return last.astimezone(UTC) + span


__all__ = [
    "MAX_RESULTS",
    "MAX_SCAN",
    "iter_starts",
    "occurrence_starts",
    "series_until_utc",
    "validate_rrule",
]
