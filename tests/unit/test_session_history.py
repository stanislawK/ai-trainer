"""The History read model (ticket #515): newest first, the time of day in the athlete's
timezone, and a UTC fallback for an unknown zone."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from ai_trainer.application.session_history import GymSessionHistory
from ai_trainer.domain.history import StoredGymSession
from ai_trainer.domain.training import SessionSpan


def _stored(day: date, first: datetime) -> StoredGymSession:
    return StoredGymSession(
        span=SessionSpan(id=uuid4(), occurred_on=day, first_logged_at=first, last_logged_at=first),
        exercises=(),
        labels={},
    )


class FakeReader:
    def __init__(self, *sessions: StoredGymSession) -> None:
        self._sessions = sessions

    async def list_sessions(self, user_id: UUID) -> Sequence[StoredGymSession]:
        return self._sessions


async def test_orders_newest_first_and_converts_to_user_timezone() -> None:
    old = _stored(date(2026, 10, 1), datetime(2026, 10, 1, 16, 30, tzinfo=UTC))
    morning = _stored(date(2026, 11, 3), datetime(2026, 11, 3, 6, 15, tzinfo=UTC))
    evening = _stored(date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC))
    history = GymSessionHistory(FakeReader(old, evening, morning))

    sessions = await history.list(uuid4(), "Europe/Warsaw")

    assert [s.id for s in sessions] == [evening.span.id, morning.span.id, old.span.id]
    assert [s.time.isoformat() for s in sessions] == ["18:40:00", "07:15:00", "18:30:00"]
    assert sessions[0].day == date(2026, 11, 3)


async def test_unknown_timezone_falls_back_to_utc() -> None:
    stored = _stored(date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC))
    history = GymSessionHistory(FakeReader(stored))

    sessions = await history.list(uuid4(), "Not/AZone")

    assert sessions[0].time.isoformat() == "17:40:00"
