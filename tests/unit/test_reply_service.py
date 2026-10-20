"""The reply use case (PRD-0003 F1, B13, B14, G3, ADR-0008 skeleton routing, ticket #82):
safety first, chitchat-like intents to the streamed chitchat specialist, every other intent to
the "not yet" reply, and nothing saved unless the whole reply came through."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.ports.conversation import ReplyContext, Routing
from ai_trainer.application.reply import (
    PART_BREAK,
    MessageNotFoundError,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    ReplyPhase,
    find_message,
    route_message,
    start_reply,
    with_screened_safety,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction
from ai_trainer.domain.training import SessionSpan
from tests.chat_support import (
    DRAFT_READY_TEXT,
    FROZEN_NOW,
    MERGE_WINDOW,
    NOT_YET_TEXT,
    THREE_6A_TWO_6B,
    UNLOGGABLE_SPORT,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    InMemoryTrainingSessions,
    ScriptedConversation,
    ScriptedFastRouter,
    ScriptedSafetyScreen,
    reply_services,
)

_HI = [Intent(kind="chitchat")]
_MINUTE = timedelta(minutes=1)
# Rowing names no extraction template, so its log gets the "not yet" reply.
_ROWED = [Intent(kind="log_session", sport=UNLOGGABLE_SPORT)]


async def _asked(chat: InMemoryChatRepository, user_id: UUID, text: str = "hi!") -> ChatMessage:
    return await chat.add(
        user_id, await chat.conversation_of(user_id), ChatRole.USER, text, FROZEN_NOW
    )


async def _events(
    chat: InMemoryChatRepository,
    user_id: UUID,
    message: ChatMessage,
    conversation: ScriptedConversation,
    *,
    drafts: InMemoryDraftRepository | None = None,
    sessions: InMemoryTrainingSessions | None = None,
    profile: InMemoryProfile | None = None,
    fast_router: ScriptedFastRouter | None = None,
    safety_screen: ScriptedSafetyScreen | None = None,
    history_turns: int = 10,
) -> list[ReplyEvent]:
    services = reply_services(
        chat,
        drafts=drafts,
        sessions=sessions,
        profile=profile,
        conversation=conversation,
        fast_router=fast_router,
        safety_screen=safety_screen,
        history_turns=history_turns,
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
    assert chat.all_of(user_id) == [asked, reply]


async def test_log_my_row_gets_the_not_yet_reply_with_no_chitchat_call() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my row")
    conversation = ScriptedConversation(intents=[_ROWED])

    events = await _events(chat, user_id, asked, conversation)

    assert conversation.chitchatted == []
    [reply] = _done(events).messages
    assert (reply.text, reply.sports) == (NOT_YET_TEXT, ("rowing",))
    assert (reply.template_id, reply.template_version) == ("not_yet", 1)
    assert _streamed_text(events) == NOT_YET_TEXT


async def test_pain_is_answered_before_the_rest_of_the_message() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my row, my knee hurts")
    conversation = ScriptedConversation(
        intents=[
            [Intent(kind="log_session", sport=UNLOGGABLE_SPORT), Intent(kind="wellbeing_or_injury")]
        ]
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
    asked = await _asked(chat, user_id, "log my row, plan next week, and my squats too")
    conversation = ScriptedConversation(
        intents=[
            [
                Intent(kind="log_session", sport=UNLOGGABLE_SPORT),
                Intent(kind="request_plan"),
                Intent(kind="log_session", sport=UNLOGGABLE_SPORT),
            ]
        ]
    )

    events = await _events(chat, user_id, asked, conversation)

    [reply] = _done(events).messages
    assert reply.sports == ("rowing",)


async def test_the_reply_reports_routing_then_replying_before_its_first_chunk() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)

    events = await _events(chat, user_id, asked, ScriptedConversation(intents=[_HI]))

    assert events[:3] == [ReplyPhase("routing"), ReplyPhase("replying"), ReplyChunk("Hey! ")]


async def test_a_router_failure_reports_only_the_routing_phase() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(route_outcomes=[LlmCallOutcome.TIMEOUT])

    events = await _events(chat, user_id, asked, conversation)

    assert events == [ReplyPhase("routing"), ReplyFailed(outcome=LlmCallOutcome.TIMEOUT)]


async def test_a_replayed_reply_reports_no_phase() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    await _events(chat, user_id, asked, ScriptedConversation(intents=[_HI]))

    replayed = await _events(chat, user_id, asked, ScriptedConversation())

    assert not any(isinstance(event, ReplyPhase) for event in replayed)


async def test_a_chitchat_timeout_saves_nothing_and_ends_with_a_failure() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(intents=[_HI], chitchat_outcomes=[LlmCallOutcome.TIMEOUT])

    events = await _events(chat, user_id, asked, conversation)

    assert events[-1] == ReplyFailed(outcome=LlmCallOutcome.TIMEOUT)
    assert chat.all_of(user_id) == [asked]


async def test_a_router_failure_ends_with_a_failure_and_no_chitchat_call() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(route_outcomes=[LlmCallOutcome.ERROR])

    events = await _events(chat, user_id, asked, conversation)

    assert events == [ReplyPhase("routing"), ReplyFailed(outcome=LlmCallOutcome.ERROR)]
    assert conversation.chitchatted == []
    assert chat.all_of(user_id) == [asked]


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
        await chat.add(
            user_id,
            await chat.conversation_of(user_id),
            ChatRole.USER,
            f"earlier {index}",
            FROZEN_NOW,
        )
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


async def test_prompt_history_holds_only_turns_from_the_messages_own_conversation() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    gym = await chat.start(user_id, "gym")
    await chat.add(user_id, gym, ChatRole.USER, "Squats 3x8 at 100 kg", FROZEN_NOW - _MINUTE * 3)
    await chat.add(user_id, gym, ChatRole.ASSISTANT, "Strong!", FROZEN_NOW - _MINUTE * 2)
    current = await chat.start(user_id, "current")
    await chat.add(user_id, current, ChatRole.USER, "Rode 40 km", FROZEN_NOW - _MINUTE)
    asked = await chat.add(user_id, current, ChatRole.USER, "And 2 more of those", FROZEN_NOW)
    conversation = ScriptedConversation(intents=[_HI])

    await _events(chat, user_id, asked, conversation)

    for context in (conversation.routed[0], conversation.chitchatted[0]):
        assert [turn.text for turn in context.history] == ["Rode 40 km"]


async def test_router_and_extraction_see_the_conversation_the_message_is_in() -> None:
    """Its calls share that conversation's OpenRouter session (ADR-0026 step 4), so the
    context names the message's own conversation, never another or the user."""
    chat, user_id = InMemoryChatRepository(), uuid4()
    other = await chat.start(user_id, "other")
    await chat.add(user_id, other, ChatRole.USER, "hi", FROZEN_NOW - _MINUTE)
    current = await chat.start(user_id, "current")
    asked = await chat.add(user_id, current, ChatRole.USER, "3 boulders at 6A", FROZEN_NOW)
    conversation = ScriptedConversation(intents=[[Intent(kind="log_session", sport="climbing")]])

    await _events(chat, user_id, asked, conversation)

    [(extracted, _)] = conversation.extracted
    assert conversation.routed[0].conversation_id == extracted.conversation_id == current
    assert current != other


async def test_the_first_message_of_a_conversation_gets_no_history() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    earlier = await chat.start(user_id, "earlier")
    await chat.add(user_id, earlier, ChatRole.USER, "Squats 3x8", FROZEN_NOW - _MINUTE)
    fresh = await chat.start(user_id, "fresh")
    asked = await chat.add(user_id, fresh, ChatRole.USER, "And 2 more of those", FROZEN_NOW)
    conversation = ScriptedConversation(intents=[_HI])

    await _events(chat, user_id, asked, conversation)

    assert list(conversation.routed[0].history) == []
    assert list(conversation.chitchatted[0].history) == []


async def test_a_saved_reply_joins_the_messages_conversation_and_moves_its_last_activity() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    hour_ago = FROZEN_NOW - _MINUTE * 60
    thread = await chat.conversations.create(user_id, "hi!", hour_ago)
    asked = await chat.add(user_id, thread.id, ChatRole.USER, "hi!", hour_ago)

    events = await _events(chat, user_id, asked, ScriptedConversation(intents=[_HI]))

    assert {reply.conversation_id for reply in _done(events).messages} == {thread.id}
    touched = await chat.conversations.get(user_id, thread.id)
    assert touched is not None
    assert touched.last_message_at == FROZEN_NOW


async def test_a_failed_reply_leaves_the_conversations_last_activity_alone() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    hour_ago = FROZEN_NOW - _MINUTE * 60
    thread = await chat.conversations.create(user_id, "hi!", hour_ago)
    asked = await chat.add(user_id, thread.id, ChatRole.USER, "hi!", hour_ago)
    failing = ScriptedConversation(route_outcomes=[LlmCallOutcome.TIMEOUT])

    await _events(chat, user_id, asked, failing)

    assert await chat.conversations.get(user_id, thread.id) == thread


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
        while not isinstance(first, ReplyChunk):
            first = await anext(events)

    assert conversation.closed_streams == 1
    assert chat.all_of(user_id) == [asked]


async def test_another_users_message_is_not_found() -> None:
    chat, owner = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, owner)
    services = reply_services(chat)

    with pytest.raises(MessageNotFoundError):
        await start_reply(uuid4(), asked.id, timezone="UTC", services=services)
    with pytest.raises(MessageNotFoundError):
        await find_message(uuid4(), asked.id, chat=chat, conversations=chat.conversations)


async def test_an_unknown_message_is_not_found() -> None:
    with pytest.raises(MessageNotFoundError):
        await start_reply(
            uuid4(), uuid4(), timezone="UTC", services=reply_services(InMemoryChatRepository())
        )


async def test_an_assistant_message_gets_no_reply_of_its_own() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    said = await chat.add(
        user_id, await chat.conversation_of(user_id), ChatRole.ASSISTANT, "Hey!", FROZEN_NOW
    )

    with pytest.raises(MessageNotFoundError):
        await start_reply(user_id, said.id, timezone="UTC", services=reply_services(chat))


async def test_find_message_returns_the_owners_own_message() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)

    assert (
        await find_message(user_id, asked.id, chat=chat, conversations=chat.conversations) == asked
    )


async def test_stopping_before_the_first_token_saves_no_reply() -> None:
    """Stop closes the stream while the router is still working (ticket #116)."""
    chat = InMemoryChatRepository()
    user_id = uuid4()
    message = await _asked(chat, user_id)
    conversation = ScriptedConversation()
    services = reply_services(chat, conversation=conversation)

    async with aclosing(
        await start_reply(user_id, message.id, timezone="UTC", services=services)
    ) as stream:
        first = await anext(stream)

    assert first == ReplyPhase("routing")
    assert [row for _, row in chat.rows] == [message]


async def test_stopping_mid_reply_saves_nothing_and_closes_the_model_stream() -> None:
    chat = InMemoryChatRepository()
    user_id = uuid4()
    message = await _asked(chat, user_id)
    conversation = ScriptedConversation()
    services = reply_services(chat, conversation=conversation)

    async with aclosing(
        await start_reply(user_id, message.id, timezone="UTC", services=services)
    ) as stream:
        async for event in stream:
            if isinstance(event, ReplyChunk):
                break

    assert [row for _, row in chat.rows] == [message]
    assert conversation.closed_streams == 1


async def test_stopping_during_routing_saves_no_reply_and_makes_no_chitchat_call() -> None:
    """The browser leaves while the router call is still in flight: the task is cancelled
    there, so the gateway can record that call as `cancelled` (ADR-0018)."""
    chat = InMemoryChatRepository()
    user_id = uuid4()
    message = await _asked(chat, user_id)
    conversation = _HangingRouter()
    services = reply_services(chat, conversation=conversation)
    stream = await start_reply(user_id, message.id, timezone="UTC", services=services)
    assert await anext(stream) == ReplyPhase("routing")

    task = asyncio.ensure_future(anext(stream))
    await conversation.routing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conversation.route_cancelled
    assert conversation.chitchatted == []
    assert [row for _, row in chat.rows] == [message]


class _HangingRouter(ScriptedConversation):
    def __init__(self) -> None:
        super().__init__()
        self.routing = asyncio.Event()
        self.route_cancelled = False

    async def route(self, context: ReplyContext) -> Routing:
        self.routing.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.route_cancelled = True
            raise
        raise AssertionError("unreachable")


# The Jev safety screen (ADR-0020, G3, ticket #121): it runs alongside the router and can only
# add the safety intent.

_KNEE = "knee's a bit sore lol, anyway hi"
_SAFETY = Intent(kind="wellbeing_or_injury")


def test_a_pain_screen_adds_the_safety_intent_the_router_missed() -> None:
    assert with_screened_safety(_HI, pain=True) == [*_HI, _SAFETY]


def test_a_pain_screen_never_duplicates_reorders_or_removes_the_routers_intents() -> None:
    routed = [Intent(kind="log_session", sport="climbing"), _SAFETY, Intent(kind="chitchat")]

    assert with_screened_safety(routed, pain=True) == routed
    assert with_screened_safety(routed, pain=False) == routed


def test_no_pain_keeps_the_routers_intents() -> None:
    assert with_screened_safety(_ROWED, pain=False) == _ROWED


async def test_a_greeting_with_pain_the_router_missed_stays_one_chitchat_reply() -> None:
    """A greeting is already pain-first: the chitchat persona answers pain first in any message
    it sees (ADR-0008), so the screen's intent changes nothing here and adds no second part."""
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, _KNEE)
    conversation = ScriptedConversation(intents=[_HI])
    screen = ScriptedSafetyScreen(pain=True)

    events = await _events(chat, user_id, asked, conversation, safety_screen=screen)

    assert screen.screened == [_KNEE]
    assert [context.message for context in conversation.chitchatted] == [_KNEE]
    [reply] = _done(events).messages
    assert reply.template_id == "chitchat"


@pytest.mark.parametrize(("pain", "first"), [(True, "chitchat"), (False, "not_yet")])
async def test_pain_the_router_missed_moves_the_greetings_chitchat_part_first(
    pain: bool, first: str
) -> None:
    """The router read the log before the greeting and missed the pain: only the screen's
    safety intent puts the pain-first chitchat part ahead of the "not yet" reply."""
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "sore knee from squats, log it, anyway hi")
    conversation = ScriptedConversation(
        intents=[[Intent(kind="log_session", sport=UNLOGGABLE_SPORT), Intent(kind="chitchat")]]
    )

    events = await _events(
        chat, user_id, asked, conversation, safety_screen=ScriptedSafetyScreen(pain=pain)
    )

    assert _done(events).messages[0].template_id == first


async def test_pain_the_router_missed_in_a_log_gets_a_chitchat_part_before_not_yet() -> None:
    """The screen's visible effect in the skeleton: without it a log-only routing gets the
    "not yet" reply alone, and nobody answers the pain."""
    chat, user_id = InMemoryChatRepository(), uuid4()
    text = "knee's sore after squats, log it"
    asked = await _asked(chat, user_id, text)
    conversation = ScriptedConversation(
        intents=[[Intent(kind="log_session", sport=UNLOGGABLE_SPORT)]]
    )
    # Slower than the router, so the reply has to wait for the screen's answer.
    screen = ScriptedSafetyScreen(pain=True, delay=0.05)

    events = await _events(chat, user_id, asked, conversation, safety_screen=screen)

    assert _streamed_text(events) == f"Hey! Good to hear from you.{PART_BREAK}{NOT_YET_TEXT}"
    assert [(m.template_id, m.sports) for m in _done(events).messages] == [
        ("chitchat", ()),
        ("not_yet", ("rowing",)),
    ]


async def test_a_message_without_pain_gets_the_routers_reply() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my row")
    screen = ScriptedSafetyScreen(pain=False)

    events = await _events(
        chat, user_id, asked, ScriptedConversation(intents=[_ROWED]), safety_screen=screen
    )

    assert screen.screened == ["log my row"]
    assert _streamed_text(events) == NOT_YET_TEXT


async def test_the_screen_runs_alongside_the_router_so_the_first_token_waits_only_for_it() -> None:
    """The router answers only once the screen has started, so a screen that ran after it
    would hang; the screen is faster, so waiting for both costs no more than the router."""
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    screen = ScriptedSafetyScreen(pain=False, delay=0.2)
    conversation = ScriptedConversation(intents=[_HI], route_after=screen.started, route_delay=0.3)
    services = reply_services(chat, conversation=conversation, safety_screen=screen)
    loop = asyncio.get_running_loop()

    started = loop.time()
    stream = await start_reply(user_id, asked.id, timezone="UTC", services=services)

    async def first_token() -> None:
        async for event in stream:
            if isinstance(event, ReplyChunk):
                return

    await asyncio.wait_for(first_token(), timeout=2)
    # Router alone is 0.3 s; the screen after the router would be 0.5 s.
    assert loop.time() - started < 0.4
    await stream.aclose()


async def test_a_screen_that_timed_out_leaves_the_router_driven_reply() -> None:
    """A timed-out or failed screen answers no pain (the adapter maps both), after its wait."""
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, _KNEE)
    screen = ScriptedSafetyScreen(pain=False, delay=0.05)

    events = await _events(
        chat, user_id, asked, ScriptedConversation(intents=[_ROWED]), safety_screen=screen
    )

    assert _streamed_text(events) == NOT_YET_TEXT
    [reply] = _done(events).messages
    assert reply.template_id == "not_yet"


async def test_a_router_failure_still_lets_the_screen_finish() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    conversation = ScriptedConversation(route_outcomes=[LlmCallOutcome.TIMEOUT])
    screen = ScriptedSafetyScreen(pain=True, delay=0.05)

    events = await _events(chat, user_id, asked, conversation, safety_screen=screen)

    assert events[-1] == ReplyFailed(outcome=LlmCallOutcome.TIMEOUT)
    assert (screen.screened, screen.cancelled) == (["hi!"], 0)


async def test_a_saved_reply_replays_without_a_screen() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id)
    await _events(chat, user_id, asked, ScriptedConversation())
    screen = ScriptedSafetyScreen()

    await _events(chat, user_id, asked, ScriptedConversation(), safety_screen=screen)

    assert screen.screened == []


async def test_stopping_during_routing_cancels_the_screen_too() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    message = await _asked(chat, user_id)
    conversation = _HangingRouter()
    screen = ScriptedSafetyScreen(delay=10)
    services = reply_services(chat, conversation=conversation, safety_screen=screen)
    stream = await start_reply(user_id, message.id, timezone="UTC", services=services)
    assert await anext(stream) == ReplyPhase("routing")

    task = asyncio.ensure_future(anext(stream))
    await conversation.routing.wait()
    await screen.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert screen.cancelled == 1


# The Jev fast path (ticket #122, ADR-0008 amendment): one confident intent skips the router.


async def test_a_fast_routed_message_is_dispatched_with_no_router_call() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my row")
    conversation = ScriptedConversation(intents=[_HI])
    fast = ScriptedFastRouter(intents=_ROWED)

    events = await _events(chat, user_id, asked, conversation, fast_router=fast)

    assert conversation.routed == []
    assert [context.message for context in fast.routed] == ["log my row"]
    assert events[0] == ReplyPhase("routing")
    [reply] = _done(events).messages
    assert (reply.text, reply.sports) == (NOT_YET_TEXT, ("rowing",))


async def test_a_message_the_fast_path_declines_goes_to_the_llm_router() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, "Ran 5k, also plan my week")
    conversation = ScriptedConversation(intents=[_ROWED, [Intent(kind="request_plan")]])
    fast = ScriptedFastRouter(intents=None)

    events = await _events(chat, user_id, asked, conversation, fast_router=fast)

    assert len(fast.routed) == 1
    assert [context.message for context in conversation.routed] == ["Ran 5k, also plan my week"]
    assert _streamed_text(events) == NOT_YET_TEXT


async def test_the_screen_adds_pain_to_a_fast_routed_message() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, _KNEE)
    screen = ScriptedSafetyScreen(pain=True)

    events = await _events(
        chat,
        user_id,
        asked,
        ScriptedConversation(),
        fast_router=ScriptedFastRouter(intents=_ROWED),
        safety_screen=screen,
    )

    assert screen.screened == [_KNEE]
    assert [message.template_id for message in _done(events).messages] == ["chitchat", "not_yet"]


# Plausible sports on the routing (ticket #123, B23).


async def _routed(fast: ScriptedFastRouter, conversation: ScriptedConversation) -> Routing:
    context = ReplyContext(
        user_id=uuid4(),
        conversation_id=uuid4(),
        message="Did intervals",
        history=(),
        athlete_sports=("gym", "cycling"),
        goals=(),
        now=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
        timezone="Europe/Warsaw",
    )
    services = reply_services(InMemoryChatRepository(), conversation=conversation, fast_router=fast)
    return await route_message(context, services)


async def test_a_fast_routed_message_carries_the_sports_jev_found_plausible() -> None:
    fast = ScriptedFastRouter(intents=_ROWED, plausible_sports=("rowing",))

    routing = await _routed(fast, ScriptedConversation())

    assert routing.plausible_sports == ("rowing",)


async def test_a_message_routed_by_the_llm_router_still_carries_jevs_plausible_sports() -> None:
    """A split-sport log is declined by the fast path, but the athlete may be asked about
    exactly the sports Jev kept in play."""
    fast = ScriptedFastRouter(intents=None, plausible_sports=("gym", "cycling"))

    routing = await _routed(fast, ScriptedConversation(intents=[_ROWED]))

    assert (routing.intents, routing.plausible_sports) == (_ROWED, ("gym", "cycling"))


async def test_a_fallback_carries_the_llm_routers_own_sport_alone() -> None:
    routing = await _routed(
        ScriptedFastRouter(intents=None), ScriptedConversation(intents=[_ROWED])
    )

    assert routing.plausible_sports == ("rowing",)


async def test_a_fallback_with_no_logged_sport_has_no_plausible_sport() -> None:
    routing = await _routed(ScriptedFastRouter(), ScriptedConversation())

    assert routing.plausible_sports == ()


async def test_an_empty_set_from_jev_falls_back_to_the_llm_routers_sport() -> None:
    """A floor above every sport leaves nothing in play; the router's own sport stands in."""
    fast = ScriptedFastRouter(intents=None, plausible_sports=())

    routing = await _routed(fast, ScriptedConversation(intents=[_ROWED]))

    assert routing.plausible_sports == ("rowing",)


_CLIMB = "I climbed 3 6A and 2 6B boulders"
_CLIMB_INTENTS = [Intent(kind="log_session", sport="climbing")]


async def test_a_climbing_log_goes_to_climbing_extract_and_saves_a_pending_draft() -> None:
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    asked = await _asked(chat, user_id, _CLIMB)
    conversation = ScriptedConversation(intents=[_CLIMB_INTENTS])

    events = await _events(chat, user_id, asked, conversation, drafts=drafts)

    assert [(context.message, sport) for context, sport in conversation.extracted] == [
        (_CLIMB, "climbing")
    ]
    assert conversation.chitchatted == []
    [draft] = drafts.all_of(user_id)
    assert (draft.message_id, draft.conversation_id, draft.sport_id, draft.status) == (
        asked.id,
        asked.conversation_id,
        "climbing",
        "pending",
    )
    assert draft.payload_version == 1
    assert (draft.draft.occurred_on, draft.draft.date_source, draft.draft.timezone) == (
        date(2026, 10, 2),
        "assumed_today",
        "Europe/Warsaw",
    )
    assert draft.draft.payload == THREE_6A_TWO_6B.to_payload().model_dump(mode="json")
    assert _done(events).drafts == (draft,)


async def test_the_draft_part_streams_its_intro_and_records_the_extraction_template() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, _CLIMB)

    events = await _events(chat, user_id, asked, ScriptedConversation(intents=[_CLIMB_INTENTS]))

    assert _streamed_text(events) == DRAFT_READY_TEXT
    [reply] = _done(events).messages
    assert (reply.text, reply.sports) == (DRAFT_READY_TEXT, ("climbing",))
    assert (reply.template_id, reply.template_version) == ("climbing.extract", 1)
    assert chat.all_of(user_id) == [asked, reply]


async def test_a_sport_without_an_extraction_template_still_gets_not_yet() -> None:
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    asked = await _asked(chat, user_id, "log my row")
    conversation = ScriptedConversation(intents=[_ROWED])

    events = await _events(chat, user_id, asked, conversation, drafts=drafts)

    assert conversation.extracted == []
    assert [m.template_id for m in _done(events).messages] == ["not_yet"]
    assert drafts.all_of(user_id) == []


@pytest.mark.parametrize("outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR])
async def test_an_extraction_failure_saves_no_draft_and_no_reply(outcome: LlmCallOutcome) -> None:
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    asked = await _asked(chat, user_id, _CLIMB)
    conversation = ScriptedConversation(intents=[_CLIMB_INTENTS], extract_outcomes=[outcome])

    events = await _events(chat, user_id, asked, conversation, drafts=drafts)

    assert events[-1] == ReplyFailed(outcome=outcome)
    assert drafts.all_of(user_id) == []
    assert chat.all_of(user_id) == [asked]


async def test_an_extraction_failure_after_a_chitchat_part_still_saves_nothing() -> None:
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    asked = await _asked(chat, user_id, f"hi! {_CLIMB}")
    conversation = ScriptedConversation(
        intents=[[Intent(kind="chitchat"), *_CLIMB_INTENTS]],
        extract_outcomes=[LlmCallOutcome.ERROR],
    )

    events = await _events(chat, user_id, asked, conversation, drafts=drafts)

    assert isinstance(events[-1], ReplyFailed)
    assert drafts.all_of(user_id) == []
    assert chat.all_of(user_id) == [asked]


async def test_a_retry_after_an_extraction_failure_saves_one_draft() -> None:
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    asked = await _asked(chat, user_id, _CLIMB)
    conversation = ScriptedConversation(
        intents=[_CLIMB_INTENTS],
        extract_outcomes=[LlmCallOutcome.TIMEOUT, LlmCallOutcome.SUCCESS],
    )

    await _events(chat, user_id, asked, conversation, drafts=drafts)
    events = await _events(chat, user_id, asked, conversation, drafts=drafts)

    assert len(_done(events).drafts) == 1
    assert len(drafts.all_of(user_id)) == 1


async def test_a_log_and_a_greeting_get_two_parts_in_message_order() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    asked = await _asked(chat, user_id, f"{_CLIMB}, also hi")
    conversation = ScriptedConversation(intents=[[*_CLIMB_INTENTS, Intent(kind="chitchat")]])

    events = await _events(chat, user_id, asked, conversation)

    assert [m.template_id for m in _done(events).messages] == ["climbing.extract", "chitchat"]
    assert _streamed_text(events) == f"{DRAFT_READY_TEXT}{PART_BREAK}Hey! Good to hear from you."


async def test_two_climbing_intents_make_one_extraction_and_one_draft() -> None:
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    asked = await _asked(chat, user_id, "3 6A boulders, then 2 6B boulders")
    conversation = ScriptedConversation(intents=[[*_CLIMB_INTENTS, *_CLIMB_INTENTS]])

    await _events(chat, user_id, asked, conversation, drafts=drafts)

    assert len(conversation.extracted) == 1
    assert len(drafts.all_of(user_id)) == 1


async def test_a_replayed_reply_carries_its_pending_drafts_with_no_model_call() -> None:
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    asked = await _asked(chat, user_id, _CLIMB)
    first = ScriptedConversation(intents=[_CLIMB_INTENTS])
    await _events(chat, user_id, asked, first, drafts=drafts)
    again = ScriptedConversation(intents=[_CLIMB_INTENTS])

    events = await _events(chat, user_id, asked, again, drafts=drafts)

    assert (again.routed, again.extracted) == ([], [])
    assert _done(events).drafts == tuple(drafts.all_of(user_id))


async def test_the_extraction_sees_the_recent_turns_and_the_athletes_timezone() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    earlier = await _asked(chat, user_id, "hey")
    asked = await chat.add(
        user_id, earlier.conversation_id, ChatRole.USER, _CLIMB, FROZEN_NOW + _MINUTE
    )
    conversation = ScriptedConversation(intents=[_CLIMB_INTENTS])

    await _events(chat, user_id, asked, conversation)

    [(context, _)] = conversation.extracted
    assert (context.message, context.timezone, context.now) == (_CLIMB, "Europe/Warsaw", FROZEN_NOW)
    assert [message.text for message in context.history] == ["hey"]


# Session continuity (B16, ADR-0014, ticket #165): the draft is offered as an addition to the
# athlete's latest same-sport session when the log came within the window of its latest log.


def _climbed_at(last_logged_at: datetime, occurred_on: date = date(2026, 10, 2)) -> SessionSpan:
    return SessionSpan(
        id=uuid4(),
        occurred_on=occurred_on,
        first_logged_at=last_logged_at - timedelta(minutes=20),
        last_logged_at=last_logged_at,
    )


async def _extends(
    session: SessionSpan | None,
    *,
    sport: str = "climbing",
    owner: UUID | None = None,
    extraction: ClimbingExtraction = THREE_6A_TWO_6B,
) -> UUID | None:
    """The `extends_session_id` the climbing log sent at `FROZEN_NOW` is stored with, when the
    latest session in `sport` of `owner` (the athlete by default) is `session`."""
    chat, drafts, user_id = InMemoryChatRepository(), InMemoryDraftRepository(), uuid4()
    sessions = InMemoryTrainingSessions()
    if session is not None:
        sessions.latest[(owner or user_id, sport)] = session
    asked = await _asked(chat, user_id, _CLIMB)
    conversation = ScriptedConversation(intents=[_CLIMB_INTENTS], extractions=[extraction])

    await _events(chat, user_id, asked, conversation, drafts=drafts, sessions=sessions)

    [draft] = drafts.all_of(user_id)
    return drafts.extends_ids[draft.id]


async def test_a_log_minutes_after_a_climbing_session_is_offered_as_an_addition_to_it() -> None:
    session = _climbed_at(FROZEN_NOW - timedelta(minutes=5))

    assert await _extends(session) == session.id


async def test_a_log_exactly_at_the_window_is_offered_and_one_second_later_is_not() -> None:
    at_the_window = _climbed_at(FROZEN_NOW - MERGE_WINDOW)
    just_past = _climbed_at(FROZEN_NOW - MERGE_WINDOW - timedelta(seconds=1))

    assert await _extends(at_the_window) == at_the_window.id
    assert await _extends(just_past) is None


async def test_a_log_a_day_after_the_last_session_starts_a_new_one_with_no_question() -> None:
    yesterday = _climbed_at(FROZEN_NOW - timedelta(days=1), occurred_on=date(2026, 10, 1))

    assert await _extends(yesterday) is None


async def test_a_session_in_another_sport_inside_the_window_is_never_offered() -> None:
    assert await _extends(_climbed_at(FROZEN_NOW - _MINUTE), sport="gym") is None


async def test_another_users_session_inside_the_window_is_never_offered() -> None:
    assert await _extends(_climbed_at(FROZEN_NOW - _MINUTE), owner=uuid4()) is None


async def test_a_log_naming_another_day_is_not_offered_even_inside_the_window() -> None:
    yesterday = THREE_6A_TWO_6B.model_copy(
        update={"occurred_on": date(2026, 10, 1), "date_source": "relative"}
    )

    assert await _extends(_climbed_at(FROZEN_NOW - _MINUTE), extraction=yesterday) is None
