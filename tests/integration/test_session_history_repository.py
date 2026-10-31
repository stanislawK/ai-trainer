"""`SqlAlchemyTrainingSessionRepository.list_sessions` on PostgreSQL (ADR-0004, ADR-0022, ticket
#378): the user's own sessions, newest day first, filtered by sport and day range."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import TrainingSessionOrm
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.training import SessionSummary
from ai_trainer.domain.users import NewUser, UserStatus
from tests.activity_support import STORES

_AT = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)


async def _user(factory: Callable[[], AsyncSession]) -> UUID:
    sub = f"history-{uuid4()}"
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    return user.id


async def _session(
    factory: Callable[[], AsyncSession], user_id: UUID, day: date, sport_id: str = "climbing"
) -> SessionSummary:
    session_id = uuid4()
    row = TrainingSessionOrm(
        id=session_id,
        user_id=user_id,
        sport_id=sport_id,
        occurred_on=day,
        timezone="Europe/Warsaw",
        duration_min=60,
        rpe=6,
        feeling="good",
        notes="steady",
        payload={"sport": sport_id, "activities": []},
        payload_version=1,
        created_at=_AT,
        updated_at=_AT,
    )
    async with factory() as db:
        db.add(row)
        await db.commit()
    return SessionSummary(
        id=session_id,
        sport_id=sport_id,
        occurred_on=day,
        duration_min=60,
        rpe=6,
        feeling="good",
        notes="steady",
    )


async def test_lists_only_the_users_sessions_newest_day_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    alice, bob = await _user(db_session_factory), await _user(db_session_factory)
    older = await _session(db_session_factory, alice, date(2026, 10, 1))
    newer = await _session(db_session_factory, alice, date(2026, 10, 9))
    await _session(db_session_factory, bob, date(2026, 10, 5))
    repo = SqlAlchemyTrainingSessionRepository(db_session_factory, stores=STORES)

    sessions = await repo.list_sessions(alice, sport_id=None, since=None, until=None, limit=10)

    assert list(sessions) == [newer, older]


async def test_filters_by_sport_and_inclusive_day_range_and_limit(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    alice = await _user(db_session_factory)
    await _session(db_session_factory, alice, date(2026, 10, 1))
    first = await _session(db_session_factory, alice, date(2026, 10, 3))
    await _session(db_session_factory, alice, date(2026, 10, 4), sport_id="gym")
    last = await _session(db_session_factory, alice, date(2026, 10, 6))
    await _session(db_session_factory, alice, date(2026, 10, 8))
    repo = SqlAlchemyTrainingSessionRepository(db_session_factory, stores=STORES)

    in_range = await repo.list_sessions(
        alice, sport_id="climbing", since=date(2026, 10, 3), until=date(2026, 10, 6), limit=10
    )
    limited = await repo.list_sessions(alice, sport_id=None, since=None, until=None, limit=2)

    assert list(in_range) == [last, first]
    assert [s.occurred_on for s in limited] == [date(2026, 10, 8), date(2026, 10, 6)]
