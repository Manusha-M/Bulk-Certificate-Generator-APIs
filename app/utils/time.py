from datetime import UTC, datetime


def utc_now() -> datetime:
    """Timezone-aware current time in UTC (stored in `timestamptz` columns)."""
    return datetime.now(UTC)
