"""`SqlAlchemyClarificationRepository` on PostgreSQL (ADR-0015, ADR-0022, ticket #162): an
open question per message, scoped to its user, resolved once, and gone with its conversation."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clarification_repository import SqlAlchemyClarificationRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.clarification import (
    ChoiceOption,
    Clarification,
    ClarificationNotFoundError,
    ClarificationResolvedError,
)
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.settings import Settings

_START = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
_LATER = datetime(2026, 10, 2, 9, 1, tzinfo=UTC)
_QUESTION = Clarification(
    question="Which sport was that?",
    options=(
        ChoiceOption(id="gym", copy_key="sport.gym.label"),
        ChoiceOption(id="cycling", copy_key="sport.cycling.label"),
    ),
    pending="log_session",
)


class _Clock:
    def now(self) -> datetime:
        return _START


async def _log(factory: Callable[[], AsyncSession], sub: str) -> tuple[UUID, UUID, UUID]:
    """A user, a conversation of theirs and their log in it."""
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    conversation = await SqlAlchemyConversationRepository(
        factory, clock=_Clock(), retention_days=14
    ).create(user.id, "Did intervals", _START)
    message = await SqlAlchemyChatRepository(factory).add(
        user.id, conversation.id, ChatRole.USER, "Did intervals", _START
    )
    return user.id, conversation.id, message.id


async def test_an_added_question_is_listed_open_with_its_options_as_given(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "clar-a")

    added = await repository.add(
        user_id, conversation_id, message_id, "sport", _QUESTION, created_at=_START
    )

    assert await repository.list_shown(user_id, conversation_id) == [added]
    assert await repository.get(user_id, added.id) == added
    assert (added.message_id, added.purpose, added.status, added.clarification) == (
        message_id,
        "sport",
        "open",
        _QUESTION,
    )
    assert added.answer_option_ids is None


async def test_another_user_never_sees_or_resolves_the_question(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    owner, conversation_id, message_id = await _log(db_session_factory, "clar-owner")
    stranger, _, _ = await _log(db_session_factory, "clar-stranger")
    added = await repository.add(
        owner, conversation_id, message_id, "sport", _QUESTION, created_at=_START
    )

    assert await repository.list_shown(stranger, conversation_id) == []
    with pytest.raises(ClarificationNotFoundError):
        await repository.get(stranger, added.id)
    with pytest.raises(ClarificationNotFoundError):
        await repository.resolve(
            stranger, added.id, "answered", option_ids=["gym"], resolved_at=_LATER
        )
    assert (await repository.get(owner, added.id)).status == "open"


async def test_a_question_cannot_block_another_users_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    owner, conversation_id, message_id = await _log(db_session_factory, "clar-fk-owner")
    stranger, _, _ = await _log(db_session_factory, "clar-fk-stranger")

    with pytest.raises(Exception, match="clarifications_conversation_id_user_id_fkey"):
        await repository.add(
            stranger, conversation_id, message_id, "sport", _QUESTION, created_at=_START
        )
    assert owner != stranger


async def test_an_answer_is_stored_once_and_a_second_one_is_refused(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "clar-once")
    added = await repository.add(
        user_id, conversation_id, message_id, "sport", _QUESTION, created_at=_START
    )

    answered = await repository.resolve(
        user_id, added.id, "answered", option_ids=["cycling"], resolved_at=_LATER
    )

    assert (answered.status, answered.answer_option_ids) == ("answered", ("cycling",))
    assert await repository.get(user_id, added.id) == answered
    with pytest.raises(ClarificationResolvedError):
        await repository.resolve(
            user_id, added.id, "answered", option_ids=["gym"], resolved_at=_LATER
        )
    with pytest.raises(ClarificationResolvedError):
        await repository.resolve(
            user_id, added.id, "abandoned", option_ids=None, resolved_at=_LATER
        )
    assert (await repository.get(user_id, added.id)).answer_option_ids == ("cycling",)


async def test_an_abandoned_question_has_no_answer(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "clar-abandon")
    added = await repository.add(
        user_id, conversation_id, message_id, "sport", _QUESTION, created_at=_START
    )

    abandoned = await repository.resolve(
        user_id, added.id, "abandoned", option_ids=None, resolved_at=_LATER
    )

    assert (abandoned.status, abandoned.answer_option_ids) == ("abandoned", None)


async def test_an_unknown_question_is_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    user_id, _, _ = await _log(db_session_factory, "clar-unknown")

    with pytest.raises(ClarificationNotFoundError):
        await repository.get(user_id, uuid4())
    with pytest.raises(ClarificationNotFoundError):
        await repository.resolve(
            user_id, uuid4(), "answered", option_ids=["gym"], resolved_at=_LATER
        )


async def test_two_answers_at_once_resolve_it_once() -> None:
    """Two answers on their own connections, committed for real (a double tap that reaches the
    server twice). The shared test connection can't race, so this test cleans up by deleting its
    user (CASCADE, G7)."""
    engine = build_engine(str(Settings().database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: UUID | None = None
    try:
        user_id, conversation_id, message_id = await _log(factory, f"clar-race-{uuid4()}")
        repository = SqlAlchemyClarificationRepository(factory)
        added = await repository.add(
            user_id, conversation_id, message_id, "sport", _QUESTION, created_at=_START
        )

        results = await asyncio.gather(
            *(
                repository.resolve(
                    user_id, added.id, "answered", option_ids=[sport], resolved_at=_LATER
                )
                for sport in ("gym", "cycling")
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(result, ClarificationResolvedError) for result in results) == 1
        assert sum(not isinstance(result, Exception) for result in results) == 1
    finally:
        if user_id is not None:
            async with factory() as db:
                await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
                await db.commit()
        await engine.dispose()


async def test_a_question_goes_with_its_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "clar-cascade")
    await repository.add(
        user_id, conversation_id, message_id, "sport", _QUESTION, created_at=_START
    )

    async with db_session_factory() as session:
        await session.execute(
            text("DELETE FROM conversations WHERE id = :id"), {"id": conversation_id}
        )
        await session.commit()

    assert await repository.list_shown(user_id, conversation_id) == []


async def test_a_reopened_answer_is_open_again_and_an_open_or_abandoned_one_is_left_alone(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyClarificationRepository(db_session_factory)
    user_id, conversation_id, message_id = await _log(db_session_factory, "clar-reopen")
    added = await repository.add(
        user_id, conversation_id, message_id, "sport", _QUESTION, created_at=_START
    )
    await repository.resolve(user_id, added.id, "answered", option_ids=["gym"], resolved_at=_LATER)

    await repository.reopen(user_id, added.id)

    reopened = await repository.get(user_id, added.id)
    assert (reopened.status, reopened.answer_option_ids) == ("open", None)
    await repository.resolve(user_id, added.id, "abandoned", option_ids=None, resolved_at=_LATER)
    await repository.reopen(user_id, added.id)
    assert (await repository.get(user_id, added.id)).status == "abandoned"
    await repository.reopen(uuid4(), added.id)
    assert (await repository.get(user_id, added.id)).status == "abandoned"
