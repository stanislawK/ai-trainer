"""`SqlAlchemySessionDraftRepository` on PostgreSQL (ADR-0022, ticket #154): drafts are per
user and per conversation, only pending ones are listed, and they go with their conversation
while saved sessions stay (B26, ADR-0022 invariant 1)."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.training import SessionDraft
from ai_trainer.domain.users import NewUser, UserStatus
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


async def _log(factory: Callable[[], AsyncSession], sub: str) -> tuple[UUID, UUID, UUID]:
    """A user, a conversation of theirs and their climbing log in it."""
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
        user.id, conversation.id, ChatRole.USER, "I climbed 3 6A and 2 6B boulders", _START
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

    assert await drafts.list_pending(user_id, conversation_id) == [added]
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

    assert await drafts.list_pending(stranger, conversation_id) == []
    assert await drafts.list_pending(stranger, own_conversation) == []


async def test_only_pending_drafts_are_listed_oldest_first(
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
        created_at=_START,
    )
    async with db_session_factory() as db:
        await db.execute(
            text("UPDATE session_drafts SET status = 'discarded' WHERE id = :id"),
            {"id": resolved.id},
        )
        await db.commit()

    assert await drafts.list_pending(user_id, conversation_id) == [earlier, later]


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
