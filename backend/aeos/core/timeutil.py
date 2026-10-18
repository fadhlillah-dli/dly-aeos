from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def age_seconds(dt: datetime | None) -> float | None:
    dt = ensure_aware(dt)
    return None if dt is None else (utcnow() - dt).total_seconds()


def is_expired(expires_at: datetime | None) -> bool:
    expires_at = ensure_aware(expires_at)
    return expires_at is not None and expires_at <= utcnow()


__all__ = ["UTC", "age_seconds", "ensure_aware", "is_expired", "timedelta", "utcnow"]
