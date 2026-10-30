"""Conversation retention (PRD-0004 B26, ADR-0021, ticket #148): a conversation past its window
is hidden at once on read, and the purge hard-deletes it, timed through the `Clock` port."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from ai_trainer.application.chat import (
    ConversationNotFoundError,
    open_conversation,
    send_message,
)
from ai_trainer.application.reply import MessageNotFoundError, find_message
from ai_trainer.application.retention import purge_expired_conversations
from ai_trainer.domain.chat import ChatRole
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryChatRepository

_RETENTION_DAYS = 14


class _Later:
    """A moment a second past the window after `FROZEN_NOW`."""

    def now(self) -> datetime:
        return FROZEN_NOW + timedelta(days=_RETENTION_DAYS, seconds=1)


async def test_a_conversation_exactly_at_the_retention_age_still_opens_and_is_listed() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    started = await chat.conversations.create(
        user_id, "kept", FROZEN_NOW - timedelta(days=_RETENTION_DAYS)
    )

    assert await open_conversation(user_id, started.id, conversations=chat.conversations) == started
    assert await chat.conversations.list_recent(user_id, 1) == [started]


async def test_a_conversation_a_second_past_the_retention_age_is_not_found_and_not_listed() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    expired = await chat.conversations.create(
        user_id, "expired", FROZEN_NOW - timedelta(days=_RETENTION_DAYS, seconds=1)
    )

    with pytest.raises(ConversationNotFoundError):
        await open_conversation(user_id, expired.id, conversations=chat.conversations)
    assert await chat.conversations.list_recent(user_id, 1) == []


async def test_sending_to_an_expired_conversation_is_not_found_and_stores_nothing() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    expired = await chat.conversations.create(
        user_id, "expired", FROZEN_NOW - timedelta(days=_RETENTION_DAYS, seconds=1)
    )

    with pytest.raises(ConversationNotFoundError):
        await send_message(
            user_id,
            expired.id,
            "hello again",
            clock=FixedClock(),
            chat=chat,
            conversations=chat.conversations,
        )

    assert chat.rows == []


async def test_a_message_of_an_expired_conversation_is_not_found_for_a_reply() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await chat.add(
        user_id, await chat.start(user_id), ChatRole.USER, "old question", FROZEN_NOW
    )
    assert (
        await find_message(user_id, asked.id, chat=chat, conversations=chat.conversations)
        is not None
    )

    chat.conversations.clock = _Later()

    with pytest.raises(MessageNotFoundError):
        await find_message(user_id, asked.id, chat=chat, conversations=chat.conversations)


async def test_the_purge_deletes_what_is_older_than_the_window_and_returns_the_count() -> None:
    chat = InMemoryChatRepository()
    first, second = uuid4(), uuid4()
    await chat.conversations.create(first, "old", FROZEN_NOW - timedelta(days=_RETENTION_DAYS + 1))
    await chat.conversations.create(second, "older", FROZEN_NOW - timedelta(days=60))
    on_the_boundary = await chat.conversations.create(
        first, "boundary", FROZEN_NOW - timedelta(days=_RETENTION_DAYS)
    )

    deleted = await purge_expired_conversations(
        clock=FixedClock(),
        conversations=chat.conversations,
        retention_days=_RETENTION_DAYS,
    )

    assert deleted == 2
    assert [c for _, c in chat.conversations.rows] == [on_the_boundary]


async def test_a_second_purge_deletes_nothing() -> None:
    chat = InMemoryChatRepository()
    await chat.conversations.create(uuid4(), "old", FROZEN_NOW - timedelta(days=30))

    async def purge() -> int:
        return await purge_expired_conversations(
            clock=FixedClock(),
            conversations=chat.conversations,
            retention_days=_RETENTION_DAYS,
        )

    assert (await purge(), await purge()) == (1, 0)
