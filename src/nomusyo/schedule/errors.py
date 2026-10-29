"""Schedule-domain exceptions. ``str(exc)`` is a short user-facing Japanese message."""

from __future__ import annotations


class ScheduleError(Exception):
    """Base class. The message is safe to show to the user (no internals)."""


class ParseError(ScheduleError, ValueError):
    """Input text is not in a supported strict format."""


class PastDateError(ScheduleError):
    """The requested date/time is in the past (and past dates are not allowed)."""


class ValidationError(ScheduleError, ValueError):
    """A value is out of range or inconsistent (title length, tz name, naive datetime...)."""


class NotFoundError(ScheduleError):
    """The target event/todo does not exist (or belongs to someone else)."""
