"""The same-session question on PostgreSQL (PRD-0004 B16, ADR-0014, ADR-0015, ADR-0022, ticket
#166): a log three hours after a same-day session is a pending draft extending nothing, with an
open `same_session` question on it. Nothing is written to sessions before the pick, a confirm
waits for it, and the pick sets or clears the session the draft extends in one transaction."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
    ReplyDone,
    ReplyServices,
    SessionSettled,
    answer_clarification,
    start_reply,
)
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.clarification import (
    NEW_SESSION_OPTION,
    ClarificationResolvedError,
    StoredClarification,
)
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import (
    DraftAwaitingAnswerError,
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
    StoredDraft,
)
from ai_trainer.domain.users import NewUser, UserStatus
from tests.chat_support import (
    MERGE_WINDOW,
    InMemoryProfile,
    ScriptedConversation,
    ScriptedFastRouter,
    ScriptedSafetyScreen,
)

# 08:00 in Warsaw (CEST, UTC+2), and a log three hours later.
_MORNING = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)
_LATER = _MORNING + timedelta(hours=3)


class _Clock:
    def __init__(self) -> None:
        self.at = _MORNING

    def now(self) -> datetime:
        return self.at


class _Athlete:
    """A climber logging through the reply on the real repositories."""

    def __init__(self, factory: Callable[[], AsyncSession], user_id: UUID, conversation_id: UUID):
        self.factory = factory
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.clock = _Clock()
        self.chat = SqlAlchemyChatRepository(factory)
        self.drafts = SqlAlchemySessionDraftRepository(factory)
        self.clarifications = SqlAlchemyClarificationRepository(factory)
        self.services = ReplyServices(
            chat=self.chat,
            conversations=SqlAlchemyConversationRepository(
                factory, clock=self.clock, retention_days=14
            ),
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=SqlAlchemyTrainingSessionRepository(factory),
            sports=default_sport_registry(),
            profile=InMemoryProfile({user_id: ["climbing"]}),
            conversation=ScriptedConversation(
                intents=[[Intent(kind="log_session", sport="climbing")]]
            ),
            fast_router=ScriptedFastRouter(),
            safety_screen=ScriptedSafetyScreen(),
            clock=self.clock,
            history_turns=10,
            merge_window=MERGE_WINDOW,
            max_clarification_options=5,
        )

    async def log(self, text_: str, at: datetime) -> ReplyDone:
        self.clock.at = at
        message = await self.chat.add(self.user_id, self.conversation_id, ChatRole.USER, text_, at)
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        *_, done = [event async for event in events]
        assert isinstance(done, ReplyDone)
        return done

    async def logged_and_confirmed(self, text_: str, at: datetime) -> StoredDraft:
        [draft] = (await self.log(text_, at)).drafts
        return await self.drafts.confirm(self.user_id, draft.id, confirmed_at=at)

    async def asked(self) -> tuple[StoredDraft, StoredDraft, StoredClarification]:
        """The morning's confirmed log, and a log three hours later with its question."""
        morning = await self.logged_and_confirmed("sent 3 6A", _MORNING)
        done = await self.log("and 2 6B", _LATER)
        [draft] = done.drafts
        [question] = done.clarifications
        return morning, draft, question

    async def answer(self, question: StoredClarification, option_id: str) -> StoredDraft:
        settled = await answer_clarification(
            self.user_id,
            question.id,
            [option_id],
            timezone="Europe/Warsaw",
            services=self.services,
        )
        assert isinstance(settled, SessionSettled)
        return settled.draft


async def _athlete(factory: Callable[[], AsyncSession], sub: str) -> _Athlete:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    conversation = await SqlAlchemyConversationRepository(
        factory, clock=_Clock(), retention_days=14
    ).create(user.id, "Climbed", _MORNING)
    return _Athlete(factory, user.id, conversation.id)


async def _row(factory: Callable[[], AsyncSession], sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).one()


async def _sessions(factory: Callable[[], AsyncSession], user_id: UUID) -> list[Any]:
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT s.id, s.payload, count(src.id) AS sources FROM training_sessions s"
                " JOIN training_session_sources src ON src.session_id = s.id"
                " WHERE s.user_id = :id GROUP BY s.id ORDER BY min(src.logged_at)"
            ),
            {"id": user_id},
        )
        return list(rows.all())


async def test_a_log_three_hours_later_is_a_draft_with_an_open_question_and_nothing_written(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-ask")

    morning, draft, question = await athlete.asked()

    assert question.purpose == "same_session"
    assert question.draft_id == draft.id
    assert [option.id for option in question.clarification.options] == [
        str(morning.session_id),
        NEW_SESSION_OPTION,
    ]
    assert question.clarification.options[0].params == {"time": "08:00"}
    stored = await _row(
        db_session_factory,
        "SELECT d.status, d.extends_session_id, d.session_id, c.status AS asked, c.draft_id"
        " FROM session_drafts d JOIN clarifications c ON c.draft_id = d.id WHERE d.id = :id",
        id=draft.id,
    )
    assert tuple(stored) == ("pending", None, None, "open", draft.id)
    [session] = await _sessions(db_session_factory, athlete.user_id)
    assert session.sources == 1
    assert [
        shown.draft_id
        for shown in await athlete.clarifications.list_shown(
            athlete.user_id, athlete.conversation_id
        )
    ] == [draft.id]


async def test_a_confirm_before_the_pick_is_refused_and_writes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-early-confirm")
    _, draft, _ = await athlete.asked()

    with pytest.raises(DraftAwaitingAnswerError):
        await athlete.drafts.confirm(athlete.user_id, draft.id, confirmed_at=_LATER)

    assert (await athlete.drafts.get(athlete.user_id, draft.id)).status == "pending"
    [session] = await _sessions(db_session_factory, athlete.user_id)
    assert session.sources == 1


async def test_picking_add_and_confirming_extends_the_earlier_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-add")
    morning, draft, question = await athlete.asked()

    settled = await athlete.answer(question, str(morning.session_id))
    confirmed = await athlete.drafts.confirm(athlete.user_id, draft.id, confirmed_at=_LATER)

    assert settled.extends is not None
    assert settled.extends.id == morning.session_id
    assert confirmed.session_id == morning.session_id
    [session] = await _sessions(db_session_factory, athlete.user_id)
    assert session.sources == 2
    assert len(session.payload["activities"]) == 4
    answered = await athlete.clarifications.get(athlete.user_id, question.id)
    assert (answered.status, answered.answer_option_ids) == (
        "answered",
        (str(morning.session_id),),
    )


async def test_picking_new_and_confirming_creates_a_second_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-new")
    morning, draft, question = await athlete.asked()

    settled = await athlete.answer(question, NEW_SESSION_OPTION)
    confirmed = await athlete.drafts.confirm(athlete.user_id, draft.id, confirmed_at=_LATER)

    assert settled.extends is None
    first, second = await _sessions(db_session_factory, athlete.user_id)
    assert (first.id, second.id) == (morning.session_id, confirmed.session_id)
    assert (first.sources, second.sources) == (1, 1)


async def test_picking_add_after_the_session_was_undone_saves_a_session_of_its_own(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-undone")
    morning, draft, question = await athlete.asked()
    await athlete.drafts.undo(athlete.user_id, morning.id, undone_at=_LATER)

    settled = await athlete.answer(question, str(morning.session_id))
    confirmed = await athlete.drafts.confirm(athlete.user_id, draft.id, confirmed_at=_LATER)

    assert settled.extends is None
    [session] = await _sessions(db_session_factory, athlete.user_id)
    assert session.id == confirmed.session_id != morning.session_id


async def test_a_question_is_answered_once(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-twice")
    morning, draft, question = await athlete.asked()
    await athlete.answer(question, NEW_SESSION_OPTION)

    with pytest.raises(ClarificationResolvedError):
        await athlete.drafts.settle_session(
            athlete.user_id,
            draft.id,
            question.id,
            str(morning.session_id),
            extends_session_id=morning.session_id,
            answered_at=_LATER,
        )

    assert (await athlete.drafts.get(athlete.user_id, draft.id)).extends is None


async def test_a_discarded_draft_is_not_settled(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-discarded")
    morning, draft, question = await athlete.asked()
    await athlete.drafts.discard(athlete.user_id, draft.id, discarded_at=_LATER)

    with pytest.raises(DraftDiscardedError):
        await athlete.answer(question, str(morning.session_id))

    assert (await athlete.clarifications.get(athlete.user_id, question.id)).status == "open"


async def test_another_users_session_is_never_extended_by_a_pick(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-tenant-a")
    other = await _athlete(db_session_factory, "same-tenant-b")
    theirs = await other.logged_and_confirmed("sent 7a", _MORNING)
    _, draft, question = await athlete.asked()

    settled = await athlete.drafts.settle_session(
        athlete.user_id,
        draft.id,
        question.id,
        NEW_SESSION_OPTION,
        extends_session_id=theirs.session_id,
        answered_at=_LATER,
    )

    assert settled.extends is None
    stored = await _row(
        db_session_factory,
        "SELECT extends_session_id FROM session_drafts WHERE id = :id",
        id=draft.id,
    )
    assert stored.extends_session_id is None


async def test_another_users_draft_and_question_are_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-tenant-c")
    other = await _athlete(db_session_factory, "same-tenant-d")
    _, draft, question = await athlete.asked()

    with pytest.raises(DraftNotFoundError):
        await other.drafts.settle_session(
            other.user_id,
            draft.id,
            question.id,
            NEW_SESSION_OPTION,
            extends_session_id=None,
            answered_at=_LATER,
        )

    assert (await athlete.clarifications.get(athlete.user_id, question.id)).status == "open"


async def test_a_question_on_another_draft_does_not_settle_this_one(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-other-draft")
    morning, _, question = await athlete.asked()
    [unrelated] = (await athlete.log("and a 6C", _LATER + timedelta(hours=1))).drafts

    with pytest.raises(ClarificationResolvedError):
        await athlete.drafts.settle_session(
            athlete.user_id,
            unrelated.id,
            question.id,
            str(morning.session_id),
            extends_session_id=morning.session_id,
            answered_at=_LATER,
        )

    assert (await athlete.clarifications.get(athlete.user_id, question.id)).status == "open"


async def test_changing_the_sport_abandons_the_open_question(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-sport-change")
    _, draft, question = await athlete.asked()

    await athlete.drafts.change_sport(
        athlete.user_id,
        draft.id,
        "gym",
        draft.draft.model_copy(
            update={"payload": {"activities": [{"name": "Deadlift", "sets": 3, "reps": 5}]}}
        ),
        payload_version=1,
        extends_session_id=None,
    )

    assert (await athlete.clarifications.get(athlete.user_id, question.id)).status == "abandoned"
    confirmed = await athlete.drafts.confirm(athlete.user_id, draft.id, confirmed_at=_LATER)
    assert confirmed.status == "confirmed"


async def test_a_confirmed_draft_is_not_settled(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-confirmed")
    morning, draft, question = await athlete.asked()
    await athlete.answer(question, NEW_SESSION_OPTION)
    await athlete.drafts.confirm(athlete.user_id, draft.id, confirmed_at=_LATER)

    with pytest.raises(DraftConfirmedError):
        await athlete.drafts.settle_session(
            athlete.user_id,
            draft.id,
            question.id,
            str(morning.session_id),
            extends_session_id=morning.session_id,
            answered_at=_LATER,
        )

    assert (await athlete.drafts.get(athlete.user_id, draft.id)).session_id != morning.session_id


async def test_a_sport_change_stores_the_new_question_in_its_own_transaction(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "same-sport-asks")
    _, draft, question = await athlete.asked()
    asked_at = _LATER + timedelta(minutes=2)

    await athlete.drafts.change_sport(
        athlete.user_id,
        draft.id,
        "gym",
        draft.draft.model_copy(
            update={"payload": {"activities": [{"name": "Deadlift", "sets": 3, "reps": 5}]}}
        ),
        payload_version=1,
        extends_session_id=None,
        asks=question.clarification,
        asked_at=asked_at,
    )

    dropped, asked = await athlete.clarifications.list_shown(
        athlete.user_id, athlete.conversation_id
    )
    assert (dropped.id, dropped.status) == (question.id, "abandoned")
    assert (asked.status, asked.draft_id, asked.purpose, asked.created_at) == (
        "open",
        draft.id,
        "same_session",
        asked_at,
    )
    with pytest.raises(DraftAwaitingAnswerError):
        await athlete.drafts.confirm(athlete.user_id, draft.id, confirmed_at=_LATER)
