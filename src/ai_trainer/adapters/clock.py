from datetime import UTC, datetime


class UtcClock:
    """Implements `ClockPort` (ADR-0014). The only place `datetime.now()` may be called."""

    def now(self) -> datetime:
        return datetime.now(UTC)
