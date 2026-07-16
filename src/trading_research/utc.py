"""Canonical UTC timestamp handling for persisted point-in-time evidence."""
from __future__ import annotations

from datetime import datetime, timezone


class TimestampError(ValueError):
    pass


def canonical_utc(value: datetime) -> datetime:
    """Return the same instant in UTC; naive values are never guessed."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TimestampError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def canonical_utc_iso(value: datetime) -> str:
    """The repository's canonical persisted representation for new evidence."""
    return canonical_utc(value).isoformat()


def parse_aware_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TimestampError("timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimestampError(f"invalid ISO-8601 timestamp {value!r}") from exc
    return canonical_utc(parsed)
