from datetime import UTC

from ai_trainer.adapters.clock import UtcClock


def test_utc_clock_returns_a_timezone_aware_utc_datetime() -> None:
    clock = UtcClock()

    now = clock.now()

    assert now.tzinfo is UTC
