"""Seeds the gym history journey's data for `tests/e2e/test_gym_history.py` and prints the
Playwright cookie as JSON (ADR-0013, ticket #519).

Run as a subprocess like `scripts/dev_session.py`, against the database the app reads
(`DATABASE_URL`). It owns a dedicated user, deleted and recreated on every run (everything
cascades from `users.id`), so the default dev user's data is never touched and every spec starts
from the same two sessions. The exercise is a custom one: the CI e2e database has no catalogue.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.orm import (
    ExerciseOrm,
    GymExerciseEntryOrm,
    GymSetOrm,
    TrainingSessionOrm,
    TrainingSessionSourceOrm,
    UserOrm,
)
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.settings import Settings
from scripts.dev_session import build_cookie_payload, get_or_create_active_user

SUB = "e2e-gym-history"
EMAIL = "e2e-gym-history@example.com"
EXERCISE_NAME = "Dumbbell lateral raise"
FIRST_DAY = date(2026, 10, 28)
SECOND_DAY = date(2026, 11, 4)


async def _log(
    factory: Callable[[], AsyncSession],
    user_id: UUID,
    exercise_id: UUID,
    day: date,
    typed: str,
    kg: Decimal,
) -> None:
    """A one-entry gym session on `day`: 4 x 10 at `kg`, typed as `typed`."""
    session_id, source_id, entry_id = uuid4(), uuid4(), uuid4()
    logged_at = datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC)
    async with factory() as db:
        db.add(
            TrainingSessionOrm(
                id=session_id,
                user_id=user_id,
                sport_id="gym",
                occurred_on=day,
                timezone="Europe/Warsaw",
                payload={},
                payload_version=2,
                created_at=logged_at,
                updated_at=logged_at,
            )
        )
        await db.flush()
        db.add(
            TrainingSessionSourceOrm(
                id=source_id,
                user_id=user_id,
                session_id=session_id,
                raw_text="log",
                source="chat",
                logged_at=logged_at,
            )
        )
        await db.flush()
        db.add(
            GymExerciseEntryOrm(
                id=entry_id,
                user_id=user_id,
                session_id=session_id,
                source_id=source_id,
                position=1,
                exercise_id=exercise_id,
                name_as_typed=typed,
                measure="load_reps",
            )
        )
        await db.flush()
        for set_no in range(1, 5):
            db.add(
                GymSetOrm(
                    user_id=user_id,
                    entry_id=entry_id,
                    set_no=set_no,
                    reps=10,
                    load_kg=kg,
                    load_original=f"{kg} kg",
                )
            )
        await db.commit()


async def main() -> None:
    settings = Settings()
    engine = build_engine(str(settings.database_url))
    try:
        factory = build_session_factory(engine)
        users = SqlAlchemyUsersRepository(factory)
        async with factory() as db:
            await db.execute(delete(UserOrm).where(UserOrm.sub == SUB))
            await db.commit()
        user = await get_or_create_active_user(users, sub=SUB, email=EMAIL)
        clock = UtcClock()
        onboarding = SqlAlchemyOnboardingRepository(factory)
        await onboarding.replace_sports(user.id, ["gym"])
        await onboarding.set_onboarded_at(user.id, clock.now())
        exercise_id = uuid4()
        async with factory() as db:
            db.add(
                ExerciseOrm(
                    id=exercise_id,
                    sport_id="gym",
                    owner_user_id=user.id,
                    source="custom",
                    name=EXERCISE_NAME,
                    default_measure="load_reps",
                )
            )
            await db.commit()
        await _log(factory, user.id, exercise_id, FIRST_DAY, "rising straight arms", Decimal(6))
        await _log(factory, user.id, exercise_id, SECOND_DAY, "rising arms to sides", Decimal(7))
        session = await SqlAlchemySessionsRepository(factory).create(
            NewSession(
                user_id=user.id,
                expires_at=clock.now() + timedelta(days=settings.session_ttl_days),
            )
        )
    finally:
        await engine.dispose()
    cookie = build_cookie_payload(session, secure=settings.session_cookie_secure)
    sys.stdout.write(json.dumps(cookie) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
