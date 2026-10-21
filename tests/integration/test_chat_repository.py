from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.users import NewUser, UserStatus
from tests.chat_support import FixedClock

_START = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


async def _user(factory: Callable[[], AsyncSession], sub: str) -> tuple[UUID, UUID]:
    """A new user and a conversation of theirs."""
    created = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    conversation = await SqlAlchemyConversationRepository(
        factory, clock=FixedClock(), retention_days=14
    ).create(created.id, "A chat", _START)
    return created.id, conversation.id


async def _another_conversation(factory: Callable[[], AsyncSession], user_id: UUID) -> UUID:
    return (
        await SqlAlchemyConversationRepository(
            factory, clock=FixedClock(), retention_days=14
        ).create(user_id, "other", _START)
    ).id


async def test_a_stored_message_is_listed_back_with_its_role_text_and_time(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-a")

    stored = await repository.add(user_id, user_id_chat, ChatRole.USER, "hello", _START)

    assert await repository.list_recent(user_id, user_id_chat, 50) == [stored]
    assert (stored.role, stored.text, stored.created_at) == (ChatRole.USER, "hello", _START)


async def test_list_recent_returns_the_latest_messages_oldest_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-b")
    for index in range(7):
        await repository.add(
            user_id, user_id_chat, ChatRole.USER, f"m{index}", _START + timedelta(minutes=index)
        )

    recent = await repository.list_recent(user_id, user_id_chat, 3)

    assert [message.text for message in recent] == ["m4", "m5", "m6"]


async def test_list_recent_never_returns_another_users_messages(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_a, user_a_chat = await _user(db_session_factory, "chat-c")
    user_b, user_b_chat = await _user(db_session_factory, "chat-d")
    await repository.add(user_a, user_a_chat, ChatRole.USER, "for a only", _START)

    assert await repository.list_recent(user_b, user_b_chat, 50) == []


async def test_deleting_a_user_cascades_their_conversations_and_chat_messages(
    db_session: AsyncSession,
) -> None:
    user_id = (
        await db_session.execute(
            text(
                "INSERT INTO users (id, sub, email) VALUES (gen_random_uuid(), 'chat-e', 'e@x.io')"
                " RETURNING id"
            )
        )
    ).scalar_one()
    conversation_id = (
        await db_session.execute(
            text(
                "INSERT INTO conversations (id, user_id, last_message_at) "
                "VALUES (gen_random_uuid(), :user_id, now()) RETURNING id"
            ),
            {"user_id": user_id},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO chat_messages (id, user_id, conversation_id, role, text) "
            "VALUES (gen_random_uuid(), :user_id, :conversation_id, 'user', 'hello')"
        ),
        {"user_id": user_id, "conversation_id": conversation_id},
    )
    await db_session.commit()

    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await db_session.commit()

    for table in ("chat_messages", "conversations"):
        remaining = await db_session.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :id"),
            {"id": user_id},
        )
        assert remaining.scalar_one() == 0, table


async def test_a_message_needs_a_conversation(db_session: AsyncSession) -> None:
    user_id = (
        await db_session.execute(
            text(
                "INSERT INTO users (id, sub, email) VALUES (gen_random_uuid(), 'chat-n', 'n@x.io')"
                " RETURNING id"
            )
        )
    ).scalar_one()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO chat_messages (id, user_id, role, text) "
                "VALUES (gen_random_uuid(), :user_id, 'user', 'orphan')"
            ),
            {"user_id": user_id},
        )
    await db_session.rollback()


async def test_a_reply_is_stored_with_its_sports_and_template(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-f")

    stored = await repository.add(
        user_id,
        user_id_chat,
        ChatRole.ASSISTANT,
        "Not yet!",
        _START,
        sports=("cycling",),
        template_id="not_yet",
        template_version=1,
    )

    assert await repository.list_recent(user_id, user_id_chat, 50) == [stored]
    assert (stored.sports, stored.template_id, stored.template_version) == (
        ("cycling",),
        "not_yet",
        1,
    )


async def test_get_finds_only_the_owners_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    owner, owner_chat = await _user(db_session_factory, "chat-g")
    stranger, _ = await _user(db_session_factory, "chat-h")
    stored = await repository.add(owner, owner_chat, ChatRole.USER, "hi!", _START)

    assert await repository.get(owner, stored.id) == stored
    assert await repository.get(stranger, stored.id) is None


async def test_list_before_returns_the_latest_earlier_messages_oldest_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-i")
    other, other_chat = await _user(db_session_factory, "chat-j")
    await repository.add(other, other_chat, ChatRole.USER, "not mine", _START)
    stored = [
        await repository.add(
            user_id, user_id_chat, ChatRole.USER, f"m{index}", _START + timedelta(minutes=index)
        )
        for index in range(5)
    ]

    before = await repository.list_before(user_id, stored[3], 2)

    assert [message.text for message in before] == ["m1", "m2"]


async def test_list_replies_returns_the_answers_up_to_the_next_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-k")
    other, other_chat = await _user(db_session_factory, "chat-l")
    asked = await repository.add(user_id, user_id_chat, ChatRole.USER, "hi!", _START)
    first = await repository.add(
        user_id, user_id_chat, ChatRole.ASSISTANT, "part 1", _START + timedelta(seconds=1)
    )
    second = await repository.add(
        user_id, user_id_chat, ChatRole.ASSISTANT, "part 2", _START + timedelta(seconds=2)
    )
    await repository.add(
        other, other_chat, ChatRole.ASSISTANT, "not mine", _START + timedelta(seconds=3)
    )
    await repository.add(
        user_id, user_id_chat, ChatRole.USER, "next", _START + timedelta(seconds=4)
    )
    await repository.add(
        user_id, user_id_chat, ChatRole.ASSISTANT, "later", _START + timedelta(seconds=5)
    )

    assert await repository.list_replies(user_id, asked) == [first, second]


async def test_list_replies_is_empty_for_an_unanswered_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-m")
    asked = await repository.add(user_id, user_id_chat, ChatRole.USER, "hi!", _START)

    assert await repository.list_replies(user_id, asked) == []


async def test_list_recent_holds_only_the_given_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-o")
    elsewhere = await _another_conversation(db_session_factory, user_id)
    await repository.add(user_id, elsewhere, ChatRole.USER, "elsewhere", _START)
    here = await repository.add(user_id, user_id_chat, ChatRole.USER, "here", _START)

    assert await repository.list_recent(user_id, user_id_chat, 50) == [here]
    assert here.conversation_id == user_id_chat


async def test_list_recent_with_another_users_conversation_is_empty(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    owner, owner_chat = await _user(db_session_factory, "chat-p")
    stranger, _ = await _user(db_session_factory, "chat-q")
    await repository.add(owner, owner_chat, ChatRole.USER, "for the owner", _START)

    assert await repository.list_recent(stranger, owner_chat, 50) == []


async def test_list_before_never_crosses_into_another_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-r")
    elsewhere = await _another_conversation(db_session_factory, user_id)
    await repository.add(user_id, elsewhere, ChatRole.USER, "Squats 3x8", _START)
    earlier = await repository.add(
        user_id, user_id_chat, ChatRole.USER, "Rode 40 km", _START + timedelta(minutes=1)
    )
    asked = await repository.add(
        user_id, user_id_chat, ChatRole.USER, "And 2 more", _START + timedelta(minutes=2)
    )

    assert await repository.list_before(user_id, asked, 10) == [earlier]


async def test_list_replies_never_takes_another_conversations_reply(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, user_id_chat = await _user(db_session_factory, "chat-s")
    elsewhere = await _another_conversation(db_session_factory, user_id)
    asked = await repository.add(user_id, user_id_chat, ChatRole.USER, "hi!", _START)
    await repository.add(
        user_id, elsewhere, ChatRole.ASSISTANT, "not this one", _START + timedelta(seconds=1)
    )

    assert await repository.list_replies(user_id, asked) == []


async def test_a_message_cannot_join_another_users_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """ADR-0021 invariant 1 holds in the schema itself: a caller that skipped the ownership
    check still couldn't put a message in someone else's conversation."""
    repository = SqlAlchemyChatRepository(db_session_factory)
    _, owner_chat = await _user(db_session_factory, "chat-t")
    intruder, _ = await _user(db_session_factory, "chat-u")

    with pytest.raises(IntegrityError):
        await repository.add(intruder, owner_chat, ChatRole.USER, "not yours", _START)


async def _failed_part(
    repository: SqlAlchemyChatRepository, user_id: UUID, conversation_id: UUID
) -> ChatMessage:
    return await repository.add(
        user_id,
        conversation_id,
        ChatRole.ASSISTANT,
        "Couldn't draft this one.",
        _START,
        sports=("cycling",),
        template_id="draft_failed",
        template_version=1,
    )


async def test_replace_part_rewrites_a_failed_part_in_place(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """A retried extraction turns its failed part into the draft's intro line (#161): same
    row, place and sports, with the text and template that now produced it."""
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, conversation_id = await _user(db_session_factory, "chat-replace")
    failed = await _failed_part(repository, user_id, conversation_id)

    replaced = await repository.replace_part(
        user_id,
        failed.id,
        expected_template_id="draft_failed",
        text="Here's the session.",
        template_id="cycling.extract",
        template_version=2,
    )

    assert replaced is not None
    assert (replaced.id, replaced.created_at, replaced.sports) == (failed.id, _START, ("cycling",))
    assert (replaced.text, replaced.template_id, replaced.template_version) == (
        "Here's the session.",
        "cycling.extract",
        2,
    )
    assert await repository.get(user_id, failed.id) == replaced


async def test_replace_part_leaves_a_part_already_replaced(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Two Retries at once rewrite the part once; the second finds it replaced."""
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id, conversation_id = await _user(db_session_factory, "chat-replace-twice")
    failed = await _failed_part(repository, user_id, conversation_id)

    async def replace() -> ChatMessage | None:
        return await repository.replace_part(
            user_id,
            failed.id,
            expected_template_id="draft_failed",
            text="Here's the session.",
            template_id="cycling.extract",
            template_version=2,
        )

    first = await replace()

    assert await replace() is None
    assert await repository.get(user_id, failed.id) == first


async def test_replace_part_never_touches_another_users_part(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    owner, conversation_id = await _user(db_session_factory, "chat-replace-owner")
    stranger, _ = await _user(db_session_factory, "chat-replace-stranger")
    failed = await _failed_part(repository, owner, conversation_id)

    replaced = await repository.replace_part(
        stranger,
        failed.id,
        expected_template_id="draft_failed",
        text="Here's the session.",
        template_id="cycling.extract",
        template_version=2,
    )

    assert replaced is None
    assert await repository.get(owner, failed.id) == failed
