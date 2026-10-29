"""Injectable clock so that all "now" logic is testable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as an aware datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Test clock. ``now`` must be aware; use :meth:`set` / :meth:`advance` to move it."""

    def __init__(self, now: datetime) -> None:
        self._now = self._check(now)

    @staticmethod
    def _check(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("FixedClock requires an aware datetime")
        return value

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = self._check(value)

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta
