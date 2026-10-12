"""The chat service (PRD-0004 F1, B13, B24, ADR-0008, ADR-0021, tickets #78 and #146): a message
is validated and stored in one of the athlete's conversations, and a conversation's latest 50
are listed back oldest first."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.chat import (
    HISTORY_LIMIT,
    MAX_MESSAGE_LENGTH,
    ConversationNotFoundError,
    EmptyMessageError,
    MessageTooLongError,
    latest_conversation,
    list_history,
    open_conversation,
    send_message,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole, conversation_title
from tests.chat_support import InMemoryChatRepository

FROZEN_NOW = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = FROZEN_NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


async def _send(
    repository: InMemoryChatRepository,
    user_id: UUID,
    text: str,
    conversation_id: UUID | None = None,
    clock: FakeClock | None = None,
) -> ChatMessage:
    return await send_message(
        user_id,
        conversation_id,
        text,
        clock=clock or FakeClock(),
        chat=repository,
        conversations=repository.conversations,
    )


async def test_send_message_stores_the_text_as_a_user_message_stamped_by_the_clock() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()

    message = await _send(repository, user_id, "hello")

    assert (message.role, message.text, message.created_at) == (ChatRole.USER, "hello", FROZEN_NOW)
    assert repository.rows == [(user_id, message)]


async def test_a_first_message_with_no_conversation_starts_one_titled_from_it() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()

    message = await _send(repository, user_id, "  Rode   90 km\nwith hills ")

    [conversation] = await repository.conversations.list_recent(user_id, 10)
    assert message.conversation_id == conversation.id
    assert conversation.title == "Rode 90 km with hills"
    assert (conversation.created_at, conversation.last_message_at) == (FROZEN_NOW, FROZEN_NOW)


async def test_sending_to_a_conversation_adds_to_it_and_moves_its_last_activity() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()
    first = await _send(repository, user_id, "first")
    later = FROZEN_NOW + timedelta(hours=2)

    second = await _send(repository, user_id, "second", first.conversation_id, FakeClock(later))

    assert second.conversation_id == first.conversation_id
    [conversation] = await repository.conversations.list_recent(user_id, 10)
    assert conversation.last_message_at == later
    assert conversation.title == "first"


async def test_sending_to_another_users_conversation_is_not_found_and_stores_nothing() -> None:
    repository = InMemoryChatRepository()
    theirs = await _send(repository, uuid4(), "theirs")

    with pytest.raises(ConversationNotFoundError):
        await _send(repository, uuid4(), "mine", theirs.conversation_id)

    assert [message.text for _, message in repository.rows] == ["theirs"]


async def test_sending_to_an_unknown_conversation_is_not_found() -> None:
    repository = InMemoryChatRepository()

    with pytest.raises(ConversationNotFoundError):
        await _send(repository, uuid4(), "hello", uuid4())

    assert repository.rows == []


async def test_send_message_trims_surrounding_whitespace() -> None:
    repository = InMemoryChatRepository()

    message = await _send(repository, uuid4(), "  hello \n")

    assert message.text == "hello"


@pytest.mark.parametrize("text", ["", " ", "\n\t  \n", "​", " ​﻿ "])
async def test_send_message_rejects_empty_or_whitespace_only_text_and_stores_nothing(
    text: str,
) -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()

    with pytest.raises(EmptyMessageError):
        await _send(repository, user_id, text)

    assert repository.rows == []
    assert await repository.conversations.list_recent(user_id, 10) == []


async def test_send_message_accepts_exactly_the_maximum_length() -> None:
    repository = InMemoryChatRepository()
    text = "a" * MAX_MESSAGE_LENGTH

    message = await _send(repository, uuid4(), text)

    assert message.text == text


async def test_send_message_rejects_text_over_the_maximum_and_stores_nothing() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()

    with pytest.raises(MessageTooLongError):
        await _send(repository, user_id, "a" * (MAX_MESSAGE_LENGTH + 1))

    assert repository.rows == []
    assert await repository.conversations.list_recent(user_id, 10) == []


async def test_the_length_limit_counts_the_trimmed_text() -> None:
    repository = InMemoryChatRepository()
    text = f"  {'a' * MAX_MESSAGE_LENGTH}  "

    message = await _send(repository, uuid4(), text)

    assert len(message.text) == MAX_MESSAGE_LENGTH


async def test_list_history_returns_the_conversations_latest_fifty_oldest_first() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()
    first = await _send(repository, user_id, "m0")
    for index in range(1, HISTORY_LIMIT + 5):
        await repository.add(
            user_id,
            first.conversation_id,
            ChatRole.USER,
            f"m{index}",
            FROZEN_NOW + timedelta(minutes=index),
        )

    history = await list_history(user_id, first.conversation_id, repository=repository)

    assert [message.text for message in history] == [f"m{i}" for i in range(5, HISTORY_LIMIT + 5)]


async def test_list_history_holds_only_that_conversations_messages() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()
    one = await _send(repository, user_id, "in one")
    other = await repository.conversations.create(user_id, "other", FROZEN_NOW)
    await _send(repository, user_id, "in other", other.id)

    history = await list_history(user_id, one.conversation_id, repository=repository)

    assert [message.text for message in history] == ["in one"]


async def test_list_history_never_returns_another_users_messages() -> None:
    repository = InMemoryChatRepository()
    secret = await _send(repository, uuid4(), "secret")

    assert await list_history(uuid4(), secret.conversation_id, repository=repository) == []


async def test_the_latest_conversation_is_the_one_with_the_latest_message() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()
    older = await _send(repository, user_id, "older")
    await _send(repository, user_id, "newer", clock=FakeClock(FROZEN_NOW + timedelta(days=1)))
    await _send(
        repository,
        user_id,
        "continued",
        older.conversation_id,
        FakeClock(FROZEN_NOW + timedelta(days=2)),
    )

    latest = await latest_conversation(user_id, conversations=repository.conversations)

    assert latest is not None
    assert latest.id == older.conversation_id


async def test_there_is_no_latest_conversation_before_the_first_message() -> None:
    repository = InMemoryChatRepository()
    await _send(repository, uuid4(), "someone else's")

    assert await latest_conversation(uuid4(), conversations=repository.conversations) is None


async def test_open_conversation_finds_only_the_owners_conversation() -> None:
    repository = InMemoryChatRepository()
    owner = uuid4()
    sent = await _send(repository, owner, "hi")

    opened = await open_conversation(
        owner, sent.conversation_id, conversations=repository.conversations
    )

    assert opened.id == sent.conversation_id
    with pytest.raises(ConversationNotFoundError):
        await open_conversation(
            uuid4(), sent.conversation_id, conversations=repository.conversations
        )


async def test_send_message_stores_line_breaks_as_lf_and_counts_them_once() -> None:
    repository = InMemoryChatRepository()
    text = "\r\n".join(["a" * 39] * 100)  # 3900 visible characters, 99 line breaks

    message = await _send(repository, uuid4(), text)

    assert "\r" not in message.text
    assert len(message.text) == 39 * 100 + 99


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("hello", "hello"),
        ("  lots \t of\n\nspace  ", "lots of space"),
        ("x" * 61, "x" * 60),
        ("word " * 20, ("word " * 12).strip()[:60]),
    ],
)
def test_a_conversation_title_is_the_text_whitespace_collapsed_and_cut_to_60(
    text: str, title: str
) -> None:
    assert conversation_title(text) == title
