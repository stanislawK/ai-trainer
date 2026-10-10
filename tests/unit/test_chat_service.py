"""The chat service (PRD-0003 F1, B13, ADR-0008, ticket #78): a message is validated, stored
per user and listed back as the latest 50, oldest first."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_trainer.application.chat import (
    HISTORY_LIMIT,
    MAX_MESSAGE_LENGTH,
    EmptyMessageError,
    MessageTooLongError,
    list_history,
    send_message,
)
from ai_trainer.domain.chat import ChatRole
from tests.chat_support import InMemoryChatRepository

FROZEN_NOW = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


async def test_send_message_stores_the_text_as_a_user_message_stamped_by_the_clock() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()

    message = await send_message(user_id, "hello", clock=FakeClock(), repository=repository)

    assert (message.role, message.text, message.created_at) == (ChatRole.USER, "hello", FROZEN_NOW)
    assert repository.rows == [(user_id, message)]


async def test_send_message_trims_surrounding_whitespace() -> None:
    repository = InMemoryChatRepository()

    message = await send_message(uuid4(), "  hello \n", clock=FakeClock(), repository=repository)

    assert message.text == "hello"


@pytest.mark.parametrize("text", ["", " ", "\n\t  \n", "\u200b", " \u200b\ufeff "])
async def test_send_message_rejects_empty_or_whitespace_only_text_and_stores_nothing(
    text: str,
) -> None:
    repository = InMemoryChatRepository()

    with pytest.raises(EmptyMessageError):
        await send_message(uuid4(), text, clock=FakeClock(), repository=repository)

    assert repository.rows == []


async def test_send_message_accepts_exactly_the_maximum_length() -> None:
    repository = InMemoryChatRepository()
    text = "a" * MAX_MESSAGE_LENGTH

    message = await send_message(uuid4(), text, clock=FakeClock(), repository=repository)

    assert message.text == text


async def test_send_message_rejects_text_over_the_maximum_and_stores_nothing() -> None:
    repository = InMemoryChatRepository()

    with pytest.raises(MessageTooLongError):
        await send_message(
            uuid4(), "a" * (MAX_MESSAGE_LENGTH + 1), clock=FakeClock(), repository=repository
        )

    assert repository.rows == []


async def test_the_length_limit_counts_the_trimmed_text() -> None:
    repository = InMemoryChatRepository()
    text = f"  {'a' * MAX_MESSAGE_LENGTH}  "

    message = await send_message(uuid4(), text, clock=FakeClock(), repository=repository)

    assert len(message.text) == MAX_MESSAGE_LENGTH


async def test_list_history_returns_the_latest_fifty_oldest_first() -> None:
    repository = InMemoryChatRepository()
    user_id = uuid4()
    for index in range(HISTORY_LIMIT + 5):
        await repository.add(
            user_id, ChatRole.USER, f"m{index}", FROZEN_NOW + timedelta(minutes=index)
        )

    history = await list_history(user_id, repository=repository)

    assert [message.text for message in history] == [f"m{i}" for i in range(5, HISTORY_LIMIT + 5)]


async def test_list_history_never_returns_another_users_messages() -> None:
    repository = InMemoryChatRepository()
    user_a, user_b = uuid4(), uuid4()
    await send_message(user_a, "secret", clock=FakeClock(), repository=repository)

    assert await list_history(user_b, repository=repository) == []


async def test_send_message_stores_line_breaks_as_lf_and_counts_them_once() -> None:
    repository = InMemoryChatRepository()
    text = "\r\n".join(["a" * 39] * 100)  # 3900 visible characters, 99 line breaks

    message = await send_message(uuid4(), text, clock=FakeClock(), repository=repository)

    assert "\r" not in message.text
    assert len(message.text) == 39 * 100 + 99
