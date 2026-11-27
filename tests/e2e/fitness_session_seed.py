"""Seeds the fitness and mobility session journey for `tests/e2e/test_fitness_session.py` and
prints the Playwright cookie as JSON (ADR-0013, ticket #634).

Run as a subprocess like `gym_history_seed.py`, against the database the app reads
(`DATABASE_URL`). It owns a dedicated user, deleted and recreated on every run. It writes an
earlier, slower rower session as typed rows, and today's conversation with a pending gym draft
(a stretch, a faster rower set and a 3-round circuit) that the browser confirms through the real
route. No model call is made: the draft is what `gym.extract` would have stored.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
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
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.sports.gym.payload import GYM_PAYLOAD_VERSION, GymPayload
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionDraft
from ai_trainer.main import build_activity_stores
from ai_trainer.settings import Settings
from scripts.dev_session import build_cookie_payload, get_or_create_active_user

SUB = "e2e-fitness-session"
EMAIL = "e2e-fitness-session@example.com"
ROWER = "Rowing machine"
EARLIER_DAY = date(2026, 11, 18)
DRAFT_DAY = date(2026, 11, 25)
LOG = "Hamstring stretch 45 s, rowed 2000 m in 8:20, 3 rounds of kettlebell swings x10"
REPLY = "Here's the session."


async def _log_earlier_rower(
    factory: Callable[[], AsyncSession], user_id: UUID, exercise_id: UUID
) -> None:
    """The slower session: 2000 m in 8:40 (520 s) on `EARLIER_DAY`."""
    session_id, source_id, entry_id = uuid4(), uuid4(), uuid4()
    logged_at = datetime(EARLIER_DAY.year, EARLIER_DAY.month, EARLIER_DAY.day, 17, 0, tzinfo=UTC)
    async with factory() as db:
        db.add(
            TrainingSessionOrm(
                id=session_id,
                user_id=user_id,
                sport_id="gym",
                occurred_on=EARLIER_DAY,
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
                name_as_typed="rowing",
                measure="distance_time",
            )
        )
        await db.flush()
        db.add(
            GymSetOrm(
                user_id=user_id,
                entry_id=entry_id,
                set_no=1,
                duration_s=520,
                distance_m=Decimal(2000),
            )
        )
        await db.commit()


def _payload(exercise_id: UUID) -> dict[str, JsonValue]:
    return GymPayload.model_validate(
        {
            "activities": [
                {
                    "name_as_typed": "Hamstring stretch",
                    "measure": "time",
                    "sets": [{"duration_s": 45}],
                },
                {
                    "name_as_typed": ROWER,
                    "exercise_id": str(exercise_id),
                    "measure": "distance_time",
                    "sets": [{"distance_m": 2000, "duration_s": 500}],
                },
                {
                    "name_as_typed": "Kettlebell swing",
                    "measure": "reps",
                    "circuit": 1,
                    "rounds": 3,
                    "sets": [{"reps": 10}] * 3,
                },
            ]
        }
    ).model_dump(mode="json")


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
                    name=ROWER,
                    default_measure="distance_time",
                )
            )
            await db.commit()
        await _log_earlier_rower(factory, user.id, exercise_id)
        now = clock.now()
        conversation = await SqlAlchemyConversationRepository(
            factory, clock=clock, retention_days=settings.conversation_retention_days
        ).create(user.id, None, now)
        chat = SqlAlchemyChatRepository(factory)
        log = await chat.add(user.id, conversation.id, ChatRole.USER, LOG, now)
        # The reply comes after the log: on a tie the chat can run a real extraction on it.
        replied = now + timedelta(seconds=1)
        await chat.add(
            user.id, conversation.id, ChatRole.ASSISTANT, REPLY, replied,
            sports=("gym",), template_id="gym.extract",
        )  # fmt: skip
        registry = default_sport_registry()
        await SqlAlchemySessionDraftRepository(
            factory, registry, stores=build_activity_stores(registry)
        ).add(
            user.id,
            conversation.id,
            log.id,
            "gym",
            SessionDraft(
                occurred_on=DRAFT_DAY,
                date_source="stated",
                timezone=user.timezone,
                duration_min=50,
                rpe=7,
                payload=_payload(exercise_id),
            ),
            payload_version=GYM_PAYLOAD_VERSION,
            created_at=replied,
        )
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
