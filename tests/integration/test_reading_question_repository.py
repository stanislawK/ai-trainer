"""The reading question on PostgreSQL (PRD-0005 B17, ADR-0015, ADR-0022, ticket #164): the
extraction sees the climbing kinds of the athlete's own recent sessions, an ambiguous log is an
open `reading` clarification with its sport in the document and nothing else written, and the
tap stores the draft in the chosen reading (ADR-0015 invariant 5). A gym or cycling question
reads the athlete's recent exercises or ride units and writes nothing else too (ticket #206)."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clarification_repository import SqlAlchemyClarificationRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.reply import (
    ClarificationAnswered,
    ReplyDone,
    ReplyServices,
    answer_clarification,
    start_reply,
)
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.clarification import UnknownOptionError
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.base import ExtractionQuestion, SessionExtraction
from ai_trainer.domain.sports.climbing.extraction import (
    ClimbingExtraction,
    ClimbingQuestion,
    ExtractedClimb,
)
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction, CyclingQuestion
from ai_trainer.domain.sports.gym.extraction import GymExtraction, GymQuestion
from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, UserStatus
from tests.activity_support import STORES
from tests.chat_support import (
    MERGE_WINDOW,
    InMemoryProfile,
    ScriptedConversation,
    ScriptedFastRouter,
    ScriptedSafetyScreen,
)

# Three days of logs, a day apart, so no log joins or asks about the one before (B16).
_FIRST = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
_DAY = timedelta(days=1)


def _climbs(kind: str, grade: str, on: datetime) -> ClimbingExtraction:
    return ClimbingExtraction(
        occurred_on=on.date(),
        date_source="assumed_today",
        activities=[ExtractedClimb(kind=kind, count=3, grade=grade)],  # type: ignore[arg-type]
    )


_BOULDER_OR_LEAD = ClimbingQuestion.model_validate(
    {"readings": [{"kind": "boulder", "grade": "7a"}, {"kind": "lead", "grade": "7a"}]}
)


class _Clock:
    def __init__(self) -> None:
        self.at = _FIRST

    def now(self) -> datetime:
        return self.at


class _Climber:
    """A climber logging through the reply on the real repositories; each log's extraction
    answers with the next of `extractions`."""

    def __init__(
        self,
        factory: Callable[[], AsyncSession],
        user_id: UUID,
        conversation_id: UUID,
        sport: str = "climbing",
    ):
        self.factory = factory
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.clock = _Clock()
        self.chat = SqlAlchemyChatRepository(factory)
        self.drafts = SqlAlchemySessionDraftRepository(factory, stores=STORES)
        self.clarifications = SqlAlchemyClarificationRepository(factory)
        self.conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport=sport)]]
        )
        self.services = ReplyServices(
            chat=self.chat,
            conversations=SqlAlchemyConversationRepository(
                factory, clock=self.clock, retention_days=14
            ),
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=SqlAlchemyTrainingSessionRepository(factory, stores=STORES),
            sports=default_sport_registry(),
            profile=InMemoryProfile({user_id: [sport]}),
            conversation=self.conversation,
            fast_router=ScriptedFastRouter(intents=[Intent(kind="log_session", sport=sport)]),
            safety_screen=ScriptedSafetyScreen(),
            clock=self.clock,
            history_turns=10,
            merge_window=MERGE_WINDOW,
            max_clarification_options=5,
            recent_sessions=10,
        )

    async def log(
        self, text_: str, at: datetime, output: SessionExtraction | ExtractionQuestion
    ) -> ReplyDone:
        self.clock.at = at
        self.conversation.extractions = [output]
        message = await self.chat.add(self.user_id, self.conversation_id, ChatRole.USER, text_, at)
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        *_, done = [event async for event in events]
        assert isinstance(done, ReplyDone)
        return done

    async def confirmed(self, text_: str, at: datetime, output: SessionExtraction) -> None:
        [draft] = (await self.log(text_, at, output)).drafts
        await self.drafts.confirm(self.user_id, draft.id, confirmed_at=at)


async def _climber(
    factory: Callable[[], AsyncSession], sub: str, sport: str = "climbing"
) -> _Climber:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    conversation = await SqlAlchemyConversationRepository(
        factory, clock=_Clock(), retention_days=14
    ).create(user.id, "Climbed", _FIRST)
    return _Climber(factory, user.id, conversation.id, sport)


async def _boulders_and_leads(climber: _Climber) -> datetime:
    """A boulder session, then a lead session a day later; returns when the next log is sent."""
    await climber.confirmed("3 6A boulders", _FIRST, _climbs("boulder", "6A", _FIRST))
    await climber.confirmed("led 3 6b", _FIRST + _DAY, _climbs("lead", "6b", _FIRST + _DAY))
    return _FIRST + 2 * _DAY


async def _count(factory: Callable[[], AsyncSession], table: str, user_id: UUID) -> int:
    async with factory() as db:
        count = await db.scalar(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :id"),
            {"id": user_id},
        )
        return count or 0


async def test_the_recent_payloads_are_the_users_own_in_the_sport_latest_day_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    climber = await _climber(db_session_factory, "reading-recent")
    stranger = await _climber(db_session_factory, "reading-stranger")
    await _boulders_and_leads(climber)
    await stranger.confirmed("top rope 5c", _FIRST, _climbs("top_rope", "5c", _FIRST))
    sessions = SqlAlchemyTrainingSessionRepository(db_session_factory, stores=STORES)

    recent = await sessions.recent_payloads(climber.user_id, "climbing", 10)

    assert [payload["activities"][0]["kind"] for payload in recent] == ["lead", "boulder"]  # type: ignore[index,call-overload]
    assert len(await sessions.recent_payloads(climber.user_id, "climbing", 1)) == 1
    assert await sessions.recent_payloads(climber.user_id, "gym", 10) == []


async def test_an_ambiguous_log_stores_only_its_question_with_its_sport(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    climber = await _climber(db_session_factory, "reading-ask")
    at = await _boulders_and_leads(climber)

    done = await climber.log("Did 3 7a", at, _BOULDER_OR_LEAD)

    assert climber.conversation.extract_options[-1] == (("lead", "boulder"), (), True)
    assert done.drafts == ()
    [question] = done.clarifications
    async with db_session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT purpose, status, draft_id, clarification FROM clarifications"
                    " WHERE id = :id"
                ),
                {"id": question.id},
            )
        ).one()
    assert (row.purpose, row.status, row.draft_id) == ("reading", "open", None)
    document: dict[str, Any] = row.clarification
    assert document["sport"] == "climbing"
    assert [option["id"] for option in document["options"]] == ["boulder", "lead"]
    # Two confirmed drafts and two sessions from before; nothing for this log.
    assert await _count(db_session_factory, "session_drafts", climber.user_id) == 2
    assert await _count(db_session_factory, "training_sessions", climber.user_id) == 2


async def test_tapping_boulders_stores_a_pending_draft_of_font_boulders(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    climber = await _climber(db_session_factory, "reading-tap")
    at = await _boulders_and_leads(climber)
    [question] = (await climber.log("Did 3 7a", at, _BOULDER_OR_LEAD)).clarifications
    climber.conversation.extractions = [_climbs("boulder", "7a", at)]

    answered = await answer_clarification(
        climber.user_id,
        question.id,
        ["boulder"],
        timezone="Europe/Warsaw",
        services=climber.services,
    )

    assert isinstance(answered, ClarificationAnswered)
    [draft] = answered.reply.drafts
    assert draft.status == "pending"
    assert draft.draft.occurred_on == date(2026, 10, 1)
    assert draft.draft.payload["activities"][0]["grade"] == {  # type: ignore[index,call-overload]
        "original": "7a",
        "scale": "font",
        "french": None,
        "ordinal": None,
    }
    stored = await climber.clarifications.get(climber.user_id, question.id)
    assert (stored.status, stored.answer_option_ids) == ("answered", ("boulder",))
    assert await _count(db_session_factory, "training_sessions", climber.user_id) == 2


async def test_an_option_the_question_never_offered_leaves_it_open_and_writes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    climber = await _climber(db_session_factory, "reading-unknown")
    at = await _boulders_and_leads(climber)
    [question] = (await climber.log("Did 3 7a", at, _BOULDER_OR_LEAD)).clarifications

    with pytest.raises(UnknownOptionError):
        await answer_clarification(
            climber.user_id,
            question.id,
            ["top_rope"],
            timezone="Europe/Warsaw",
            services=climber.services,
        )

    assert (await climber.clarifications.get(climber.user_id, question.id)).status == "open"
    assert await _count(db_session_factory, "session_drafts", climber.user_id) == 2


def _ride(unit: str | None, on: datetime) -> CyclingExtraction:
    return CyclingExtraction.model_validate(
        {
            "occurred_on": on.date().isoformat(),
            "date_source": "assumed_today",
            "activities": [{"distance": {"original": "40", "value": 40, "unit": unit}}],
        }
    )


def _lift(name: str, unit: str | None, on: datetime) -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": on.date().isoformat(),
            "date_source": "assumed_today",
            "activities": [
                {
                    "name": name,
                    "sets": 5,
                    "reps": 5,
                    "load": {"original": "60", "value": 60, "unit": unit},
                }
            ],
        }
    )


async def test_a_ride_whose_unit_is_unclear_stores_only_its_question(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """AC4 for cycling: the rides were in miles, then km."""
    rider = await _climber(db_session_factory, "reading-ride", "cycling")
    await rider.confirmed("40 mi", _FIRST, _ride("mi", _FIRST))
    await rider.confirmed("40 km", _FIRST + _DAY, _ride("km", _FIRST + _DAY))
    question = CyclingQuestion.model_validate({"distance": "40", "readings": ["km", "mi"]})

    done = await rider.log("Rode 40", _FIRST + 2 * _DAY, question)

    assert rider.conversation.extract_options[-1] == (("km", "mi"), (), True)
    assert done.drafts == ()
    [stored] = done.clarifications
    assert (stored.purpose, stored.status, stored.clarification.sport) == (
        "reading",
        "open",
        "cycling",
    )
    assert [option.id for option in stored.clarification.options] == ["km", "mi"]
    assert await _count(db_session_factory, "session_drafts", rider.user_id) == 2
    assert await _count(db_session_factory, "training_sessions", rider.user_id) == 2


async def test_tapping_a_movement_stores_a_pending_draft_of_that_movement(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """AC1 and AC4 for gym: nothing but the question until the tap, then the named draft."""
    lifter = await _climber(db_session_factory, "reading-lift", "gym")
    await lifter.confirmed("bench 5x5 60kg", _FIRST, _lift("bench press", "kg", _FIRST))
    at = _FIRST + _DAY
    question = GymQuestion.model_validate(
        {"about": "movement", "exercise": "press", "readings": ["overhead press", "bench press"]}
    )
    [asked] = (await lifter.log("Did 5x5 press at 60", at, question)).clarifications
    assert lifter.conversation.extract_options[-1][0] == ("bench press (kg)",)
    assert await _count(db_session_factory, "session_drafts", lifter.user_id) == 1
    assert await _count(db_session_factory, "training_sessions", lifter.user_id) == 1
    lifter.conversation.extractions = [_lift("press", None, at)]

    answered = await answer_clarification(
        lifter.user_id,
        asked.id,
        ["overhead press"],
        timezone="Europe/Warsaw",
        services=lifter.services,
    )

    assert isinstance(answered, ClarificationAnswered)
    [draft] = answered.reply.drafts
    assert draft.status == "pending"
    [exercise] = GymPayload.model_validate(draft.draft.payload).activities
    assert exercise.name == "overhead press"
    assert await _count(db_session_factory, "training_sessions", lifter.user_id) == 1
