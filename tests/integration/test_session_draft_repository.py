"""`SqlAlchemySessionDraftRepository` on PostgreSQL (ADR-0022, ticket #154): drafts are per
user and per conversation, drafts of every status are listed, and they go with their
conversation while saved sessions stay (B26, ADR-0022 invariant 1)."""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.retention import purge_expired_conversations
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.sports.cycling.payload import CYCLING_PAYLOAD_VERSION
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.payload import GYM_PAYLOAD_VERSION
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
    SessionDraft,
    SessionNotFoundError,
    StoredDraft,
)
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.settings import Settings
from tests.chat_support import THREE_6A_TWO_6B

_START = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
_DRAFT = SessionDraft(
    occurred_on=date(2026, 10, 2),
    date_source="assumed_today",
    timezone="Europe/Warsaw",
    payload=THREE_6A_TWO_6B.to_payload().model_dump(mode="json"),
)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


async def _log(
    factory: Callable[[], AsyncSession], sub: str, log: str = "I climbed 3 6A and 2 6B boulders"
) -> tuple[UUID, UUID, UUID]:
    """A user, a conversation of theirs and their log in it, a climbing one by default."""
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    conversations = SqlAlchemyConversationRepository(
        factory, clock=_Clock(_START), retention_days=14
    )
    conversation = await conversations.create(user.id, "Climbed", _START)
    message = await SqlAlchemyChatRepository(factory).add(
        user.id, conversation.id, ChatRole.USER, log, _START
    )
    return user.id, conversation.id, message.id


async def test_an_added_draft_is_listed_pending_with_its_draft_as_given(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "draft-a")

    added = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START,
    )

    assert await drafts.list_shown(user_id, conversation_id) == [added]
    assert (added.message_id, added.sport_id, added.status, added.draft) == (
        message_id,
        "climbing",
        "pending",
        _DRAFT,
    )


async def test_another_user_never_lists_the_draft(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    owner, conversation_id, message_id = await _log(db_session_factory, "draft-owner")
    stranger, own_conversation, _ = await _log(db_session_factory, "draft-stranger")
    await drafts.add(
        owner, conversation_id, message_id, "climbing", _DRAFT, payload_version=1, created_at=_START
    )

    assert await drafts.list_shown(stranger, conversation_id) == []
    assert await drafts.list_shown(stranger, own_conversation) == []


async def test_a_users_other_conversation_never_lists_the_draft(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """A follow-up log takes its sport only from its own conversation's drafts (#189)."""
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "draft-two-chats")
    conversations = SqlAlchemyConversationRepository(
        db_session_factory, clock=_Clock(_START), retention_days=14
    )
    other = await conversations.create(user_id, "Lifted", _START)
    await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START,
    )

    assert await drafts.list_shown(user_id, other.id) == []


async def test_discarded_drafts_are_listed_with_the_rest_oldest_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "draft-pending")
    later = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START + timedelta(minutes=2),
    )
    earlier = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START,
    )
    resolved = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START + timedelta(minutes=1),
    )
    async with db_session_factory() as db:
        await db.execute(
            text("UPDATE session_drafts SET status = 'discarded' WHERE id = :id"),
            {"id": resolved.id},
        )
        await db.commit()

    assert await drafts.list_shown(user_id, conversation_id) == [
        earlier,
        resolved.model_copy(update={"status": "discarded"}),
        later,
    ]


async def test_adding_a_draft_writes_no_training_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "draft-no-session")

    await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START,
    )

    async with db_session_factory() as db:
        sessions = await db.scalar(
            text("SELECT count(*) FROM training_sessions WHERE user_id = :id"), {"id": user_id}
        )
        sources = await db.scalar(
            text("SELECT count(*) FROM training_session_sources WHERE user_id = :id"),
            {"id": user_id},
        )
    assert (sessions, sources) == (0, 0)


_INSERT_SESSION = text(
    "INSERT INTO training_sessions (id, user_id, sport_id, occurred_on, timezone, payload,"
    " payload_version, created_at, updated_at) VALUES (:id, :user_id, 'climbing', :day,"
    " 'Europe/Warsaw', '{}', 1, :at, :at)"
)
_INSERT_SOURCE = text(
    "INSERT INTO training_session_sources (id, user_id, session_id, raw_text, source, logged_at)"
    " VALUES (:id, :user_id, :session_id, 'I climbed', 'chat', :at)"
)
_INSERT_CLARIFICATION = text(
    "INSERT INTO clarifications (id, user_id, conversation_id, message_id, draft_id, purpose,"
    " clarification, status, created_at) VALUES (:id, :user_id, :conversation_id, :message_id,"
    " :draft_id, 'sport', '{}', 'open', :at)"
)


async def test_purging_a_conversation_takes_its_drafts_and_questions_but_keeps_sessions(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "draft-purge")
    draft = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START,
    )
    session_id = uuid4()
    async with db_session_factory() as db:
        await db.execute(
            _INSERT_SESSION,
            {"id": session_id, "user_id": user_id, "day": _START.date(), "at": _START},
        )
        await db.execute(
            _INSERT_SOURCE,
            {"id": uuid4(), "user_id": user_id, "session_id": session_id, "at": _START},
        )
        await db.execute(
            _INSERT_CLARIFICATION,
            {
                "id": uuid4(),
                "user_id": user_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "draft_id": draft.id,
                "at": _START,
            },
        )
        await db.execute(
            text("UPDATE session_drafts SET session_id = :session_id WHERE id = :id"),
            {"session_id": session_id, "id": draft.id},
        )
        await db.commit()
    later = _START + timedelta(days=15)
    conversations = SqlAlchemyConversationRepository(
        db_session_factory, clock=_Clock(later), retention_days=14
    )

    await conversations.purge_before(later - timedelta(days=14))

    async with db_session_factory() as db:
        counts = [
            await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE user_id = :id"), {"id": user_id}
            )
            for table in (
                "session_drafts",
                "clarifications",
                "training_sessions",
                "training_session_sources",
            )
        ]
    assert counts == [0, 0, 1, 1]


async def _pending(
    factory: Callable[[], AsyncSession], sub: str
) -> tuple[SqlAlchemySessionDraftRepository, UUID, StoredDraft]:
    drafts = SqlAlchemySessionDraftRepository(factory)
    user_id, conversation_id, message_id = await _log(factory, sub)
    draft = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START,
    )
    return drafts, user_id, draft


async def _counts(factory: Callable[[], AsyncSession], user_id: UUID) -> tuple[int, int]:
    async with factory() as db:
        sessions = await db.scalar(
            text("SELECT count(*) FROM training_sessions WHERE user_id = :id"), {"id": user_id}
        )
        sources = await db.scalar(
            text("SELECT count(*) FROM training_session_sources WHERE user_id = :id"),
            {"id": user_id},
        )
    return sessions or 0, sources or 0


async def test_confirm_writes_one_session_and_its_source_and_marks_the_draft_confirmed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "confirm-ok")
    confirmed_at = _START + timedelta(minutes=5)

    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=confirmed_at)

    assert (confirmed.id, confirmed.status) == (draft.id, "confirmed")
    assert confirmed.session_id is not None
    async with db_session_factory() as db:
        session = (
            await db.execute(
                text(
                    "SELECT user_id, sport_id, occurred_on, timezone, payload, payload_version,"
                    " created_at, updated_at FROM training_sessions WHERE id = :id"
                ),
                {"id": confirmed.session_id},
            )
        ).one()
        source = (
            await db.execute(
                text(
                    "SELECT user_id, raw_text, source, logged_at FROM training_session_sources"
                    " WHERE session_id = :id"
                ),
                {"id": confirmed.session_id},
            )
        ).one()
        resolved_at = await db.scalar(
            text("SELECT resolved_at FROM session_drafts WHERE id = :id"), {"id": draft.id}
        )
    assert tuple(session) == (
        user_id,
        "climbing",
        _DRAFT.occurred_on,
        "Europe/Warsaw",
        _DRAFT.payload,
        1,
        confirmed_at,
        confirmed_at,
    )
    assert tuple(source) == (user_id, "I climbed 3 6A and 2 6B boulders", "chat", _START)
    assert resolved_at == confirmed_at


async def test_confirming_a_ride_in_miles_saves_it_in_km_with_the_log_as_typed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """A cycling draft confirms like a climbing one (ticket #160): its payload keeps the miles
    as typed beside the km, and its source row keeps the athlete's text (B2)."""
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(
        db_session_factory, "confirm-ride", "Rode 40 miles yesterday"
    )
    payload = CyclingExtraction.model_validate(
        {
            "occurred_on": "2026-10-01",
            "date_source": "relative",
            "activities": [{"distance": {"original": "40 miles", "value": 40, "unit": "mi"}}],
        }
    ).to_payload()
    draft = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "cycling",
        _DRAFT.model_copy(
            update={"occurred_on": date(2026, 10, 1), "payload": payload.model_dump(mode="json")}
        ),
        payload_version=CYCLING_PAYLOAD_VERSION,
        created_at=_START,
    )

    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    async with db_session_factory() as db:
        sport_id, stored, raw_text = (
            await db.execute(
                text(
                    "SELECT s.sport_id, s.payload, src.raw_text FROM training_sessions s"
                    " JOIN training_session_sources src ON src.session_id = s.id"
                    " WHERE s.id = :id"
                ),
                {"id": confirmed.session_id},
            )
        ).one()
    assert (sport_id, raw_text) == ("cycling", "Rode 40 miles yesterday")
    assert stored["activities"][0]["distance"] == {
        "original": "40 miles",
        "value": 40,
        "unit": "mi",
        "km": 64.37,
    }


async def test_confirming_lifts_in_lb_saves_them_in_kg_with_the_log_as_typed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """A gym draft confirms like a climbing one (ticket #159): its payload keeps the pounds as
    typed beside the kg, and its source row keeps the athlete's text (B2)."""
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(
        db_session_factory, "confirm-lifts", "Deadlift 3x5 at 315 lb, then 3 sets of pull-ups"
    )
    payload = GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-01",
            "date_source": "relative",
            "activities": [
                {
                    "name": "Deadlift",
                    "sets": 3,
                    "reps": 5,
                    "load": {"original": "315 lb", "value": 315, "unit": "lb"},
                },
                {"name": "pull-ups", "sets": 3},
            ],
        }
    ).to_payload()
    draft = await drafts.add(
        user_id,
        conversation_id,
        message_id,
        "gym",
        _DRAFT.model_copy(
            update={"occurred_on": date(2026, 10, 1), "payload": payload.model_dump(mode="json")}
        ),
        payload_version=GYM_PAYLOAD_VERSION,
        created_at=_START,
    )

    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    async with db_session_factory() as db:
        sport_id, stored, raw_text = (
            await db.execute(
                text(
                    "SELECT s.sport_id, s.payload, src.raw_text FROM training_sessions s"
                    " JOIN training_session_sources src ON src.session_id = s.id"
                    " WHERE s.id = :id"
                ),
                {"id": confirmed.session_id},
            )
        ).one()
    assert (sport_id, raw_text) == ("gym", "Deadlift 3x5 at 315 lb, then 3 sets of pull-ups")
    assert [exercise["name"] for exercise in stored["activities"]] == ["Deadlift", "pull-ups"]
    assert stored["activities"][0]["load"] == {
        "original": "315 lb",
        "value": 315,
        "unit": "lb",
        "kg": 142.88,
    }


async def test_confirm_copies_every_envelope_field_the_athlete_gave(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts = SqlAlchemySessionDraftRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "confirm-envelope")
    full = _DRAFT.model_copy(
        update={
            "started_at": _START - timedelta(hours=2),
            "duration_min": 90,
            "rpe": 7,
            "feeling": "strong",
            "notes": "fingers tired at the end",
        }
    )
    draft = await drafts.add(
        user_id, conversation_id, message_id, "climbing", full, payload_version=1, created_at=_START
    )

    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    async with db_session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT started_at, duration_min, rpe, feeling, notes FROM training_sessions"
                    " WHERE id = :id"
                ),
                {"id": confirmed.session_id},
            )
        ).one()
    assert tuple(row) == (
        _START - timedelta(hours=2),
        90,
        7,
        "strong",
        "fingers tired at the end",
    )


async def test_confirming_twice_returns_the_same_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "confirm-twice")

    first = await drafts.confirm(user_id, draft.id, confirmed_at=_START)
    second = await drafts.confirm(user_id, draft.id, confirmed_at=_START + timedelta(seconds=1))

    assert second == first
    assert await _counts(db_session_factory, user_id) == (1, 1)


class _RacingSession(AsyncSession):
    """Waits after its first query (the confirm's read of the draft) until the other confirm has
    read it too, or a short time has passed. Without a row lock both read `pending` and both
    write; with it, the second read blocks in PostgreSQL until the first confirm commits."""

    barrier: asyncio.Barrier

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        result = await super().scalar(*args, **kwargs)
        if not self.info.get("raced"):
            self.info["raced"] = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.barrier.wait(), timeout=0.5)
        return result


async def test_concurrent_confirms_create_one_session() -> None:
    """Two confirms on their own connections, committed for real (a double tap that reaches the
    server twice). The shared test connection can't race, so this test cleans up by deleting its
    user (CASCADE, G7)."""
    engine = build_engine(str(Settings().database_url))
    setup = async_sessionmaker(engine, expire_on_commit=False)
    _RacingSession.barrier = asyncio.Barrier(2)
    racing = async_sessionmaker(engine, class_=_RacingSession, expire_on_commit=False)
    user_id: UUID | None = None
    try:
        _, user_id, draft = await _pending(setup, f"confirm-race-{uuid4()}")
        drafts = SqlAlchemySessionDraftRepository(racing)

        results = await asyncio.gather(
            drafts.confirm(user_id, draft.id, confirmed_at=_START),
            drafts.confirm(user_id, draft.id, confirmed_at=_START),
        )

        assert results[0].session_id == results[1].session_id
        assert await _counts(setup, user_id) == (1, 1)
    finally:
        if user_id is not None:
            async with setup() as db:
                await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
                await db.commit()
        await engine.dispose()


async def test_another_users_draft_is_not_found_and_nothing_is_written(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, owner, draft = await _pending(db_session_factory, "confirm-owner")
    stranger, _, _ = await _log(db_session_factory, "confirm-stranger")

    with pytest.raises(DraftNotFoundError):
        await drafts.confirm(stranger, draft.id, confirmed_at=_START)

    assert await drafts.list_shown(owner, draft.conversation_id) == [draft]
    assert await _counts(db_session_factory, owner) == (0, 0)
    assert await _counts(db_session_factory, stranger) == (0, 0)


async def test_an_unknown_draft_is_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, _ = await _pending(db_session_factory, "confirm-unknown")

    with pytest.raises(DraftNotFoundError):
        await drafts.confirm(user_id, uuid4(), confirmed_at=_START)


async def test_confirming_a_discarded_draft_is_refused_and_writes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "confirm-discarded")
    async with db_session_factory() as db:
        await db.execute(
            text("UPDATE session_drafts SET status = 'discarded' WHERE id = :id"),
            {"id": draft.id},
        )
        await db.commit()

    with pytest.raises(DraftDiscardedError):
        await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    assert await _counts(db_session_factory, user_id) == (0, 0)
    async with db_session_factory() as db:
        status = await db.scalar(
            text("SELECT status FROM session_drafts WHERE id = :id"), {"id": draft.id}
        )
    assert status == "discarded"


async def test_a_confirmed_draft_is_listed_with_its_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "confirm-listed")

    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    assert await drafts.list_shown(user_id, draft.conversation_id) == [confirmed]


async def test_a_confirmed_session_survives_the_purge_of_its_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "confirm-purge")
    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)
    later = _Clock(_START + timedelta(days=15))
    conversations = SqlAlchemyConversationRepository(
        db_session_factory, clock=later, retention_days=14
    )

    # The purge the `purge-conversations` CLI runs (ADR-0021).
    await purge_expired_conversations(clock=later, conversations=conversations, retention_days=14)

    assert await _counts(db_session_factory, user_id) == (1, 1)
    async with db_session_factory() as db:
        raw_text = await db.scalar(
            text("SELECT raw_text FROM training_session_sources WHERE session_id = :id"),
            {"id": confirmed.session_id},
        )
        drafts_left = await db.scalar(
            text("SELECT count(*) FROM session_drafts WHERE user_id = :id"), {"id": user_id}
        )
    assert (raw_text, drafts_left) == ("I climbed 3 6A and 2 6B boulders", 0)


async def test_discard_marks_the_draft_discarded_and_writes_no_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "discard-ok")
    discarded_at = _START + timedelta(minutes=5)

    discarded = await drafts.discard(user_id, draft.id, discarded_at=discarded_at)

    assert (discarded.id, discarded.status, discarded.session_id) == (draft.id, "discarded", None)
    async with db_session_factory() as db:
        row = (
            await db.execute(
                text("SELECT status, resolved_at, session_id FROM session_drafts WHERE id = :id"),
                {"id": draft.id},
            )
        ).one()
    assert tuple(row) == ("discarded", discarded_at, None)
    assert await _counts(db_session_factory, user_id) == (0, 0)
    assert await drafts.list_shown(user_id, draft.conversation_id) == [discarded]


async def test_discarding_twice_keeps_the_first_time(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "discard-twice")
    first_at = _START + timedelta(minutes=5)
    first = await drafts.discard(user_id, draft.id, discarded_at=first_at)

    second = await drafts.discard(user_id, draft.id, discarded_at=first_at + timedelta(hours=1))

    assert second == first
    async with db_session_factory() as db:
        resolved_at = await db.scalar(
            text("SELECT resolved_at FROM session_drafts WHERE id = :id"), {"id": draft.id}
        )
    assert resolved_at == first_at


async def test_discarding_another_users_draft_is_not_found_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, owner, draft = await _pending(db_session_factory, "discard-owner")
    stranger, _, _ = await _log(db_session_factory, "discard-stranger")

    with pytest.raises(DraftNotFoundError):
        await drafts.discard(stranger, draft.id, discarded_at=_START)

    assert await drafts.list_shown(owner, draft.conversation_id) == [draft]


async def test_discarding_an_unknown_draft_is_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, _ = await _pending(db_session_factory, "discard-unknown")

    with pytest.raises(DraftNotFoundError):
        await drafts.discard(user_id, uuid4(), discarded_at=_START)


async def test_discarding_a_confirmed_draft_is_refused_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "discard-confirmed")
    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    with pytest.raises(DraftConfirmedError):
        await drafts.discard(user_id, draft.id, discarded_at=_START + timedelta(minutes=1))

    assert await drafts.list_shown(user_id, draft.conversation_id) == [confirmed]
    assert await _counts(db_session_factory, user_id) == (1, 1)


async def test_undo_deletes_the_session_and_its_sources_and_keeps_the_draft_confirmed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "undo-ok")
    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)
    assert confirmed.session_id is not None

    undone = await drafts.undo(user_id, draft.id, undone_at=_START)

    assert (undone.id, undone.status, undone.session_id) == (draft.id, "confirmed", None)
    assert await _counts(db_session_factory, user_id) == (0, 0)
    async with db_session_factory() as db:
        row = (
            await db.execute(
                text("SELECT status, session_id FROM session_drafts WHERE id = :id"),
                {"id": draft.id},
            )
        ).one()
    assert tuple(row) == ("confirmed", None)


async def test_another_users_session_is_not_found_by_undo_and_stays(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, owner, draft = await _pending(db_session_factory, "undo-owner")
    stranger, _, _ = await _log(db_session_factory, "undo-stranger")
    confirmed = await drafts.confirm(owner, draft.id, confirmed_at=_START)
    assert confirmed.session_id is not None

    with pytest.raises(DraftNotFoundError):
        await drafts.undo(stranger, draft.id, undone_at=_START)

    assert await _counts(db_session_factory, owner) == (1, 1)
    (kept,) = await drafts.list_shown(owner, draft.conversation_id)
    assert kept.session_id == confirmed.session_id


async def test_an_unknown_draft_or_an_already_removed_session_is_not_found_by_undo(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "undo-twice")
    await drafts.confirm(user_id, draft.id, confirmed_at=_START)
    await drafts.undo(user_id, draft.id, undone_at=_START)

    with pytest.raises(SessionNotFoundError):
        await drafts.undo(user_id, draft.id, undone_at=_START)
    with pytest.raises(DraftNotFoundError):
        await drafts.undo(user_id, uuid4(), undone_at=_START)


async def test_undo_leaves_the_users_other_sessions(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, first = await _pending(db_session_factory, "undo-other")
    second = await drafts.add(
        user_id,
        first.conversation_id,
        first.message_id,
        "climbing",
        _DRAFT,
        payload_version=1,
        created_at=_START,
    )
    kept = await drafts.confirm(user_id, second.id, confirmed_at=_START)
    await drafts.confirm(user_id, first.id, confirmed_at=_START)

    await drafts.undo(user_id, first.id, undone_at=_START)

    assert await _counts(db_session_factory, user_id) == (1, 1)
    shown = {d.id: d.session_id for d in await drafts.list_shown(user_id, first.conversation_id)}
    assert shown == {first.id: None, second.id: kept.session_id}


async def test_get_returns_the_users_draft_whatever_its_status(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "get-ok")
    assert await drafts.get(user_id, draft.id) == draft

    discarded = await drafts.discard(user_id, draft.id, discarded_at=_START)

    assert await drafts.get(user_id, draft.id) == discarded


async def test_get_of_another_users_or_an_unknown_draft_is_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, _, draft = await _pending(db_session_factory, "get-owner")
    stranger, _, _ = await _log(db_session_factory, "get-stranger")

    with pytest.raises(DraftNotFoundError):
        await drafts.get(stranger, draft.id)
    with pytest.raises(DraftNotFoundError):
        await drafts.get(stranger, uuid4())


_YESTERDAY_EDIT = _DRAFT.model_copy(
    update={"occurred_on": date(2026, 10, 1), "date_source": "stated", "rpe": 7}
)


async def test_an_edited_draft_stays_pending_and_its_confirm_saves_the_edit(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "edit-ok")

    edited = await drafts.update(user_id, draft.id, _YESTERDAY_EDIT, payload_version=1)
    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    assert (edited.id, edited.status, edited.draft) == (draft.id, "pending", _YESTERDAY_EDIT)
    assert await drafts.get(user_id, draft.id) == confirmed
    async with db_session_factory() as db:
        session = (
            await db.execute(
                text("SELECT occurred_on, rpe FROM training_sessions WHERE id = :id"),
                {"id": confirmed.session_id},
            )
        ).one()
    assert tuple(session) == (date(2026, 10, 1), 7)


async def test_editing_another_users_or_an_unknown_draft_is_not_found_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, owner, draft = await _pending(db_session_factory, "edit-owner")
    stranger, _, _ = await _log(db_session_factory, "edit-stranger")

    with pytest.raises(DraftNotFoundError):
        await drafts.update(stranger, draft.id, _YESTERDAY_EDIT, payload_version=1)
    with pytest.raises(DraftNotFoundError):
        await drafts.update(owner, uuid4(), _YESTERDAY_EDIT, payload_version=1)

    assert await drafts.get(owner, draft.id) == draft


async def test_editing_a_confirmed_draft_is_refused_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "edit-confirmed")
    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    with pytest.raises(DraftConfirmedError):
        await drafts.update(user_id, draft.id, _YESTERDAY_EDIT, payload_version=1)

    assert await drafts.get(user_id, draft.id) == confirmed


async def test_editing_a_discarded_draft_is_refused_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "edit-discarded")
    discarded = await drafts.discard(user_id, draft.id, discarded_at=_START)

    with pytest.raises(DraftDiscardedError):
        await drafts.update(user_id, draft.id, _YESTERDAY_EDIT, payload_version=1)

    assert await drafts.get(user_id, draft.id) == discarded


async def test_editing_an_undone_draft_is_refused_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "edit-undone")
    confirmed = await drafts.confirm(user_id, draft.id, confirmed_at=_START)
    assert confirmed.session_id is not None
    undone = await drafts.undo(user_id, draft.id, undone_at=_START)

    with pytest.raises(DraftConfirmedError):
        await drafts.update(user_id, draft.id, _YESTERDAY_EDIT, payload_version=1)

    assert await drafts.get(user_id, draft.id) == undone
    assert await _counts(db_session_factory, user_id) == (0, 0)
