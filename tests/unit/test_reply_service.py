"""The reply use case (PRD-0003 F1, B13, B14, G3, ADR-0008 skeleton routing, ticket #82):
safety first, chitchat-like intents to the streamed chitchat specialist, every other intent to
the "not yet" reply, and nothing saved unless the whole reply came through."""

from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import date
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.reply import (
    MessageNotFoundError,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    find_message,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.llm_calls import LlmCallOutcome
from tests.chat_support import (
    FROZEN_NOW,
    NOT_YET_TEXT,
    InMemoryChatRepository,
    InMemoryProfile,
    ScriptedConversation,
    reply_services,
)

_HI = [Intent(kind="chitchat")]
_RIDE = [Intent(kind="log_session", sport="cycling")]


async def _asked(chat: InMemoryChatRepository, user_id: UUID, text: str = "hi!") -> ChatMessage:
    return await chat.add(user_id, ChatRole.USER, text, FROZEN_NOW)


async def _events(
    chat: InMemoryChatRepository,
    user_id: UUID,
    message: ChatMessage,
    conversation: ScriptedConversation,
    *,
    profile: InMemoryProfile | None = None,
    history_turns: int = 10,
) -> list[ReplyEvent]:
    services = reply_services(
        chat, profile=profile, conversation=conversation, history_turns=history_turns
    )
    events = await start_reply(user_id, message.id, timezone="Europe/Warsaw", services=services)
    return [event async for event in events]


def _streamed_text(events: list[ReplyEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, ReplyChunk))


def _done(events: list[ReplyEvent]) -> ReplyDone:
    assert isinstance(events[-1], ReplyDone)
    return events[-1]


async def test_hi_streams_the_chitchat_reply_and_saves_it_for_the_next_page_load() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(intents=[_HI])

    events = await _events(chat, user_id, asked, conversation)

    assert _streamed_text(events) == "Hey! Good to hear from you."
    [reply] = _done(events).messages
    assert (reply.role, reply.text, reply.sports) == (
        ChatRole.ASSISTANT,
        "Hey! Good to hear from you.",
        (),
    )
    assert (reply.template_id, reply.template_version) == ("chitchat", 1)
    assert await chat.list_recent(user_id, 50) == [asked, reply]


async def test_log_my_ride_gets_the_not_yet_reply_with_no_chitchat_call() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my ride")
    conversation = ScriptedConversation(intents=[_RIDE])

    events = await _events(chat, user_id, asked, conversation)

    assert conversation.chitchatted == []
    [reply] = _done(events).messages
    assert (reply.text, reply.sports) == (NOT_YET_TEXT, ("cycling",))
    assert (reply.template_id, reply.template_version) == ("not_yet", 1)
    assert _streamed_text(events) == NOT_YET_TEXT


async def test_pain_is_answered_before_the_rest_of_the_message() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my ride, my knee hurts")
    conversation = ScriptedConversation(
        intents=[[Intent(kind="log_session", sport="cycling"), Intent(kind="wellbeing_or_injury")]]
    )

    events = await _events(chat, user_id, asked, conversation)

    replies = _done(events).messages
    assert [reply.template_id for reply in replies] == ["chitchat", "not_yet"]
    assert _streamed_text(events) == "Hey! Good to hear from you.\n\n" + NOT_YET_TEXT
    assert replies[0].created_at < replies[1].created_at


async def test_my_knee_hurts_also_hi_makes_one_chitchat_call_that_sees_the_whole_message() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "my knee hurts, also hi")
    conversation = ScriptedConversation(
        intents=[[Intent(kind="wellbeing_or_injury"), Intent(kind="chitchat")]]
    )

    events = await _events(chat, user_id, asked, conversation)

    assert [context.message for context in conversation.chitchatted] == ["my knee hurts, also hi"]
    assert len(_done(events).messages) == 1


@pytest.mark.parametrize("kind", ["chitchat", "unclear", "wellbeing_or_injury"])
async def test_skeleton_routing_sends_these_intents_to_chitchat(kind: str) -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(intents=[[Intent(kind=kind)]])  # type: ignore[arg-type]

    events = await _events(chat, user_id, asked, conversation)

    assert len(conversation.chitchatted) == 1
    assert [reply.template_id for reply in _done(events).messages] == ["chitchat"]


async def test_several_not_yet_intents_share_one_not_yet_reply_with_their_sports() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my ride and my climb, and plan next week")
    conversation = ScriptedConversation(
        intents=[
            [
                Intent(kind="log_session", sport="cycling"),
                Intent(kind="log_session", sport="climbing"),
                Intent(kind="log_session", sport="cycling"),
                Intent(kind="request_plan"),
            ]
        ]
    )

    events = await _events(chat, user_id, asked, conversation)

    [reply] = _done(events).messages
    assert reply.sports == ("cycling", "climbing")


async def test_a_chitchat_timeout_saves_nothing_and_ends_with_a_failure() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(intents=[_HI], chitchat_outcomes=[LlmCallOutcome.TIMEOUT])

    events = await _events(chat, user_id, asked, conversation)

    assert events[-1] == ReplyFailed(outcome=LlmCallOutcome.TIMEOUT)
    assert await chat.list_recent(user_id, 50) == [asked]


async def test_a_router_failure_ends_with_a_failure_and_no_chitchat_call() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(route_outcomes=[LlmCallOutcome.ERROR])

    events = await _events(chat, user_id, asked, conversation)

    assert events == [ReplyFailed(outcome=LlmCallOutcome.ERROR)]
    assert conversation.chitchatted == []
    assert await chat.list_recent(user_id, 50) == [asked]


async def test_a_retry_after_a_timeout_streams_a_fresh_reply() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(
        intents=[_HI], chitchat_outcomes=[LlmCallOutcome.TIMEOUT, LlmCallOutcome.SUCCESS]
    )

    await _events(chat, user_id, asked, conversation)
    retried = await _events(chat, user_id, asked, conversation)

    assert len(conversation.chitchatted) == 2
    assert [reply.text for reply in _done(retried).messages] == ["Hey! Good to hear from you."]


async def test_an_answered_message_replays_its_saved_reply_with_no_model_call() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    first = await _events(chat, user_id, asked, ScriptedConversation(intents=[_HI]))
    again = ScriptedConversation()

    replayed = await _events(chat, user_id, asked, again)

    assert replayed == [ReplyDone(messages=_done(first).messages)]
    assert (again.routed, again.chitchatted) == ([], [])


async def test_router_and_chitchat_see_the_recent_turns_profile_and_time() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    for index in range(4):
        await chat.add(user_id, ChatRole.USER, f"earlier {index}", FROZEN_NOW)
    asked = await _asked(chat, user_id)
    goal = Goal(text="Send my first 7a", target_date=date(2027, 6, 1), sport_id="climbing")
    profile = InMemoryProfile(sports={user_id: ["climbing"]}, goals={user_id: [goal]})
    conversation = ScriptedConversation(intents=[_HI])

    await _events(chat, user_id, asked, conversation, profile=profile, history_turns=2)

    for context in (conversation.routed[0], conversation.chitchatted[0]):
        assert context.user_id == user_id
        assert context.message == "hi!"
        assert [turn.text for turn in context.history] == ["earlier 2", "earlier 3"]
        assert (list(context.athlete_sports), list(context.goals)) == (["climbing"], [goal])
        assert (context.now, context.timezone) == (FROZEN_NOW, "Europe/Warsaw")


async def test_reply_parts_are_stamped_after_the_message_even_on_a_frozen_clock() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(
        intents=[[Intent(kind="chitchat"), Intent(kind="request_report")]]
    )

    events = await _events(chat, user_id, asked, conversation)

    stamps = [asked.created_at, *(reply.created_at for reply in _done(events).messages)]
    assert stamps == sorted(set(stamps))


async def test_closing_the_reply_early_closes_the_chitchat_stream() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(intents=[_HI])
    services = reply_services(chat, conversation=conversation)
    events: AsyncGenerator[ReplyEvent] = await start_reply(
        user_id, asked.id, timezone="UTC", services=services
    )

    async with aclosing(events):
        first = await anext(events)

    assert isinstance(first, ReplyChunk)
    assert conversation.closed_streams == 1
    assert await chat.list_recent(user_id, 50) == [asked]


async def test_another_users_message_is_not_found() -> None:
    chat, owner = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, owner)
    services = reply_services(chat)

    with pytest.raises(MessageNotFoundError):
        await start_reply(uuid4(), asked.id, timezone="UTC", services=services)
    with pytest.raises(MessageNotFoundError):
        await find_message(uuid4(), asked.id, chat=chat)


async def test_an_unknown_message_is_not_found() -> None:
    with pytest.raises(MessageNotFoundError):
        await start_reply(
            uuid4(), uuid4(), timezone="UTC", services=reply_services(InMemoryChatRepository())
        )


async def test_an_assistant_message_gets_no_reply_of_its_own() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    said = await chat.add(user_id, ChatRole.ASSISTANT, "Hey!", FROZEN_NOW)

    with pytest.raises(MessageNotFoundError):
        await start_reply(user_id, said.id, timezone="UTC", services=reply_services(chat))


async def test_find_message_returns_the_owners_own_message() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)

    assert await find_message(user_id, asked.id, chat=chat) == asked
