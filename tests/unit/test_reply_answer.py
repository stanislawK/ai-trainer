"""How a question reaches a sport's answer specialist (PRD 0011 B40, G3, ADR-0008 amendment at
#421's plan gate): Jev names the sport, an athlete's only sport stands in when it can't, and
otherwise the question gets "not yet", with no guess and no card."""

from uuid import UUID, uuid4

from ai_trainer.application.reply import (
    PART_BREAK,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent, IntentKind
from ai_trainer.domain.llm_calls import LlmCallOutcome
from tests.chat_support import (
    FROZEN_NOW,
    NOT_YET_TEXT,
    InMemoryChatRepository,
    InMemoryProfile,
    ScriptedConversation,
    ScriptedQuestionSport,
    reply_services,
)

_ANSWER = "Your bench is 45 kg."
_ASK = [Intent(kind="ask_training_question")]


async def _reply(
    text: str,
    intents: list[Intent],
    *,
    picked: str | None = None,
    sports: list[str] | None = None,
    conversation: ScriptedConversation | None = None,
    question_sport: ScriptedQuestionSport | None = None,
    chat: InMemoryChatRepository | None = None,
) -> tuple[list[ReplyEvent], ScriptedConversation, ScriptedQuestionSport, UUID, ChatMessage]:
    chat, user_id = chat or InMemoryChatRepository(), uuid4()
    conversation = conversation or ScriptedConversation()
    conversation.intents = [intents]
    question_sport = question_sport or ScriptedQuestionSport(sport=picked)
    message = await chat.add(
        user_id, await chat.conversation_of(user_id), ChatRole.USER, text, FROZEN_NOW
    )
    services = reply_services(
        chat,
        profile=InMemoryProfile(sports={user_id: sports or []}),
        conversation=conversation,
        question_sport=question_sport,
    )
    events = await start_reply(user_id, message.id, timezone="Europe/Warsaw", services=services)
    return [event async for event in events], conversation, question_sport, user_id, message


def _streamed(events: list[ReplyEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, ReplyChunk))


def _done(events: list[ReplyEvent]) -> ReplyDone:
    assert isinstance(events[-1], ReplyDone)
    return events[-1]


async def test_a_bench_question_streams_from_the_gym_specialist_and_is_saved() -> None:
    events, conversation, asked, user_id, _ = await _reply(
        "How is my bench going?", _ASK, picked="gym"
    )

    assert asked.asked == ["How is my bench going?"]
    assert [sport for _, sport in conversation.answered] == ["gym"]
    assert conversation.answered[0][0].user_id == user_id
    assert conversation.chitchatted == []
    assert [event.text for event in events if isinstance(event, ReplyChunk)] == [
        "Your bench ",
        "is 45 kg.",
    ]
    (reply,) = _done(events).messages
    assert (reply.text, reply.sports) == (_ANSWER, ("gym",))
    assert (reply.template_id, reply.template_version) == ("gym.answer", 1)


async def test_a_report_of_one_exercise_goes_to_the_specialist_too() -> None:
    events, conversation, _, _, _ = await _reply(
        "How is my bench going?", [Intent(kind="request_report")], picked="gym"
    )

    assert [sport for _, sport in conversation.answered] == ["gym"]
    assert _done(events).messages[0].template_id == "gym.answer"


async def test_a_question_and_a_report_in_one_message_share_one_answer() -> None:
    events, conversation, asked, _, _ = await _reply(
        "How often should I deload, and how is my bench?",
        [Intent(kind="ask_training_question"), Intent(kind="request_report")],
        picked="gym",
    )

    assert len(asked.asked) == 1
    assert len(conversation.answered) == 1
    assert len(_done(events).messages) == 1


async def test_a_deload_question_goes_to_the_gym_specialist() -> None:
    _, conversation, _, _, _ = await _reply(
        "How often should I deload?", _ASK, picked="gym", sports=["gym", "climbing"]
    )

    assert [sport for _, sport in conversation.answered] == ["gym"]


async def test_a_climbing_question_gets_not_yet_with_its_sport() -> None:
    events, conversation, _, _, _ = await _reply(
        "How often should I hangboard?", _ASK, picked="climbing", sports=["gym", "climbing"]
    )

    assert conversation.answered == []
    (reply,) = _done(events).messages
    assert (reply.text, reply.sports, reply.template_id) == (
        NOT_YET_TEXT,
        ("climbing",),
        "not_yet",
    )


async def test_a_cycling_question_gets_not_yet_even_from_a_gym_athlete() -> None:
    events, conversation, _, _, _ = await _reply(
        "What cadence should I ride at?", _ASK, picked="cycling", sports=["gym"]
    )

    assert conversation.answered == []
    assert _done(events).messages[0].sports == ("cycling",)


async def test_a_gym_only_athletes_unplaced_question_goes_to_the_specialist() -> None:
    _, conversation, _, _, _ = await _reply("And last week?", _ASK, sports=["gym"])

    assert [sport for _, sport in conversation.answered] == ["gym"]


async def test_a_question_naming_no_sport_from_a_climbing_athlete_gets_not_yet() -> None:
    events, conversation, _, _, _ = await _reply("And last week?", _ASK, sports=["climbing"])

    assert conversation.answered == []
    (reply,) = _done(events).messages
    assert (reply.template_id, reply.sports) == ("not_yet", ("climbing",))


async def test_a_multi_sport_athletes_unplaced_question_gets_not_yet() -> None:
    events, conversation, _, _, _ = await _reply(
        "How was my week?", _ASK, sports=["gym", "climbing"]
    )

    assert conversation.answered == []
    (reply,) = _done(events).messages
    assert (reply.template_id, reply.sports) == ("not_yet", ())


async def test_a_question_from_an_athlete_with_no_sports_gets_not_yet() -> None:
    events, conversation, _, _, _ = await _reply("And last week?", _ASK, sports=[])

    assert conversation.answered == []
    assert _done(events).messages[0].template_id == "not_yet"


async def test_pain_with_a_gym_question_is_answered_first() -> None:
    events, conversation, _, _, _ = await _reply(
        "My shoulder hurts, how is my bench going?",
        [Intent(kind="wellbeing_or_injury"), Intent(kind="ask_training_question")],
        picked="gym",
    )

    assert len(conversation.chitchatted) == 1
    assert len(conversation.answered) == 1
    assert _streamed(events) == f"Hey! Good to hear from you.{PART_BREAK}{_ANSWER}"
    assert [reply.template_id for reply in _done(events).messages] == ["chitchat", "gym.answer"]


async def test_pain_the_router_listed_last_still_comes_before_the_answer() -> None:
    events, _, _, _, _ = await _reply(
        "How is my bench going? My shoulder hurts",
        [Intent(kind="ask_training_question"), Intent(kind="wellbeing_or_injury")],
        picked="gym",
    )

    assert [reply.template_id for reply in _done(events).messages] == ["chitchat", "gym.answer"]


async def test_a_log_and_a_question_get_a_draft_and_an_answer() -> None:
    events, conversation, _, _, _ = await _reply(
        "Climbed 3 6A, how is my bench going?",
        [Intent(kind="log_session", sport="climbing"), Intent(kind="ask_training_question")],
        picked="gym",
    )

    assert [sport for _, sport in conversation.extracted] == ["climbing"]
    assert [sport for _, sport in conversation.answered] == ["gym"]
    assert len(_done(events).messages) == 2


async def test_a_log_turn_never_asks_which_sport_a_question_is_about() -> None:
    """G14: a log pays for no question it doesn't hold."""
    _, _, asked, _, _ = await _reply(
        "Climbed 3 6A", [Intent(kind="log_session", sport="climbing")], picked="gym"
    )

    assert asked.asked == []


async def test_a_plan_request_never_asks_either() -> None:
    kind: IntentKind = "request_plan"
    events, _, asked, _, _ = await _reply("Plan my week", [Intent(kind=kind)], picked="gym")

    assert asked.asked == []
    assert _done(events).messages[0].template_id == "not_yet"


async def test_a_failed_answer_ends_the_reply_and_saves_nothing() -> None:
    conversation = ScriptedConversation(answer_outcomes=[LlmCallOutcome.ERROR])
    chat = InMemoryChatRepository()

    events, _, _, user_id, message = await _reply(
        "How is my bench going?", _ASK, picked="gym", conversation=conversation, chat=chat
    )

    assert events[-1] == ReplyFailed(outcome=LlmCallOutcome.ERROR)
    assert not any(isinstance(event, ReplyDone) for event in events)
    assert chat.all_of(user_id) == [message]


async def test_a_rate_limited_answer_ends_the_reply_as_rate_limited() -> None:
    conversation = ScriptedConversation(answer_outcomes=[LlmCallOutcome.RATE_LIMITED])

    events, _, _, _, _ = await _reply(
        "How is my bench going?", _ASK, picked="gym", conversation=conversation
    )

    assert events[-1] == ReplyFailed(outcome=LlmCallOutcome.RATE_LIMITED)
