"""Asking which sport (PRD-0004 B17, B23, F9, ADR-0015, ticket #162): a log whose sport stays
split between two or more of the athlete's sports gets a choice card, not a guess. Asking
writes no draft and no session, the answer is option IDs, and the resumed run drafts in the
chosen sport."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.reply import (
    ClarificationAnswered,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    abandon_clarification,
    answer_clarification,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    ClarificationNotFoundError,
    ClarificationResolvedError,
    StoredClarification,
    UnknownOptionError,
)
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from tests.chat_support import (
    DRAFT_READY_TEXT,
    FROZEN_NOW,
    SPORT_QUESTION_TEXT,
    InMemoryChatRepository,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    ScriptedFastRouter,
    ScriptedSafetyScreen,
    reply_services,
)

_LIFTER_RIDER = ["gym", "cycling"]
_INTERVALS = "Did intervals"


def _split_log(*plausible: str) -> ScriptedFastRouter:
    """Jev is sure of one log but not of its sport."""
    return ScriptedFastRouter(intents=None, plausible_sports=plausible, single_intent="log_session")


class _Setup:
    def __init__(self, *, sports: list[str] = _LIFTER_RIDER, **services: object) -> None:
        self.chat = InMemoryChatRepository()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.user_id = uuid4()
        self.conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]]
        )
        self.fast = _split_log(*_LIFTER_RIDER)
        self.safety = ScriptedSafetyScreen()
        self.profile = InMemoryProfile({self.user_id: sports})
        self.max_options = 5
        for name, value in services.items():
            setattr(self, name, value)

    @property
    def services(self):  # type: ignore[no-untyped-def]
        return reply_services(
            self.chat,
            drafts=self.drafts,
            clarifications=self.clarifications,
            profile=self.profile,
            conversation=self.conversation,
            fast_router=self.fast,
            safety_screen=self.safety,
            max_clarification_options=self.max_options,
        )

    async def ask(self, text: str = _INTERVALS) -> tuple[ChatMessage, list[ReplyEvent]]:
        conversation_id = await self.chat.conversation_of(self.user_id)
        message = await self.chat.add(
            self.user_id, conversation_id, ChatRole.USER, text, FROZEN_NOW
        )
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        return message, [event async for event in events]


def _done(events: list[ReplyEvent]) -> ReplyDone:
    assert isinstance(events[-1], ReplyDone)
    return events[-1]


async def _open_question(setup: _Setup) -> tuple[ChatMessage, StoredClarification]:
    message, events = await setup.ask()
    [question] = _done(events).clarifications
    return message, question


async def test_a_log_split_between_gym_and_cycling_asks_with_exactly_those_two_options() -> None:
    setup = _Setup()

    message, events = await setup.ask()

    done = _done(events)
    [reply] = done.messages
    assert (reply.text, reply.template_id, reply.template_version) == (
        SPORT_QUESTION_TEXT,
        "sport_question",
        1,
    )
    [question] = done.clarifications
    assert question.purpose == "sport"
    assert question.status == "open"
    assert question.message_id == message.id
    assert [option.id for option in question.clarification.options] == ["gym", "cycling"]
    assert question.clarification.question == SPORT_QUESTION_TEXT
    assert question.clarification.allow_free_text


async def test_asking_makes_no_router_call_no_extraction_and_no_draft() -> None:
    setup = _Setup()

    _, events = await setup.ask()

    assert (setup.conversation.routed, setup.conversation.extracted) == ([], [])
    assert (_done(events).drafts, setup.drafts.all_of(setup.user_id)) == ((), [])


async def test_the_options_are_only_the_plausible_sports_most_likely_first() -> None:
    setup = _Setup(sports=["climbing", "gym", "cycling"], fast=_split_log("cycling", "climbing"))

    _, events = await setup.ask()

    [question] = _done(events).clarifications
    assert [option.id for option in question.clarification.options] == ["cycling", "climbing"]


async def test_an_option_carries_its_sports_copy_key() -> None:
    setup = _Setup()

    _, events = await setup.ask()

    [question] = _done(events).clarifications
    assert [option.copy_key for option in question.clarification.options] == [
        "sport.gym.label",
        "sport.cycling.label",
    ]


async def test_more_plausible_sports_than_the_limit_are_cut_to_the_likeliest() -> None:
    setup = _Setup(
        sports=["climbing", "gym", "cycling"],
        fast=_split_log("gym", "cycling", "climbing"),
        max_options=2,
    )

    _, events = await setup.ask()

    [question] = _done(events).clarifications
    assert [option.id for option in question.clarification.options] == ["gym", "cycling"]


async def test_a_sport_that_cannot_be_logged_is_not_offered() -> None:
    setup = _Setup(sports=["gym", "cycling", "rowing"], fast=_split_log("rowing", "gym", "cycling"))

    _, events = await setup.ask()

    [question] = _done(events).clarifications
    assert [option.id for option in question.clarification.options] == ["gym", "cycling"]


async def test_one_loggable_plausible_sport_is_no_question_and_goes_to_the_llm_router() -> None:
    setup = _Setup(sports=["gym", "rowing"], fast=_split_log("rowing", "gym"))

    _, events = await setup.ask()

    assert _done(events).clarifications == ()
    assert [sport for _, sport in setup.conversation.extracted] == ["gym"]
    assert len(setup.conversation.routed) == 1


async def test_a_sport_jev_is_sure_of_is_never_asked_about() -> None:
    setup = _Setup(
        fast=ScriptedFastRouter(
            intents=[Intent(kind="log_session", sport="cycling")],
            plausible_sports=("gym", "cycling"),
            single_intent="log_session",
        )
    )

    _, events = await setup.ask()

    assert _done(events).clarifications == ()
    assert [sport for _, sport in setup.conversation.extracted] == ["cycling"]


async def test_a_pain_mention_alongside_a_split_sport_is_answered_not_asked_about() -> None:
    """Safety comes first (ADR-0008 invariant 4): the log is drafted in its likeliest sport,
    which the athlete can still change on the card."""
    setup = _Setup(safety=ScriptedSafetyScreen(pain=True))

    _, events = await setup.ask("Did intervals, my knee hurts")

    done = _done(events)
    assert done.clarifications == ()
    assert [draft.sport_id for draft in done.drafts] == ["gym"]
    assert len(setup.conversation.chitchatted) == 1


async def test_a_reload_after_asking_replays_the_question_with_no_second_clarification() -> None:
    setup = _Setup()
    message, first = await setup.ask()

    again = await start_reply(
        setup.user_id, message.id, timezone="Europe/Warsaw", services=setup.services
    )
    replay = [event async for event in again]

    assert _done(replay).messages == _done(first).messages
    assert _done(replay).clarifications == _done(first).clarifications
    assert len(setup.clarifications.all_of(setup.user_id)) == 1


async def test_tapping_cycling_drafts_cycling_and_answers_the_question() -> None:
    setup = _Setup()
    message, question = await _open_question(setup)

    answered = await answer_clarification(
        setup.user_id, question.id, ["cycling"], timezone="Europe/Warsaw", services=setup.services
    )

    assert isinstance(answered, ClarificationAnswered)
    assert (answered.clarification.status, answered.clarification.answer_option_ids) == (
        "answered",
        ("cycling",),
    )
    [intro] = answered.reply.messages
    assert (intro.text, intro.template_id) == (DRAFT_READY_TEXT, "cycling.extract")
    [draft] = answered.reply.drafts
    assert (draft.sport_id, draft.message_id, draft.status) == ("cycling", message.id, "pending")
    [(context, sport)] = setup.conversation.extracted
    assert (context.message, sport) == (_INTERVALS, "cycling")
    assert setup.conversation.routed == []


async def test_the_resumed_reply_sorts_after_the_question_in_the_conversation() -> None:
    setup = _Setup()
    _, question = await _open_question(setup)

    answered = await answer_clarification(
        setup.user_id, question.id, ["gym"], timezone="Europe/Warsaw", services=setup.services
    )

    assert isinstance(answered, ClarificationAnswered)
    thread = [m for m in setup.chat.all_of(setup.user_id)]
    assert [m.text for m in thread] == [_INTERVALS, SPORT_QUESTION_TEXT, DRAFT_READY_TEXT]
    assert thread[1].created_at < thread[2].created_at


async def test_nothing_is_written_to_drafts_before_the_tap() -> None:
    setup = _Setup()

    await setup.ask()

    assert setup.drafts.all_of(setup.user_id) == []


@pytest.mark.parametrize(
    "option_ids",
    [["rowing"], ["swimming"], [], ["gym", "cycling"], [""], ["Cycling"]],
    ids=["unlisted", "unknown", "none", "two in single mode", "blank", "label text"],
)
async def test_an_option_id_the_question_never_offered_is_rejected_and_writes_nothing(
    option_ids: list[str],
) -> None:
    setup = _Setup()
    _, question = await _open_question(setup)

    with pytest.raises(UnknownOptionError):
        await answer_clarification(
            setup.user_id,
            question.id,
            option_ids,
            timezone="Europe/Warsaw",
            services=setup.services,
        )

    assert setup.conversation.extracted == []
    assert setup.drafts.all_of(setup.user_id) == []
    assert (await setup.clarifications.get(setup.user_id, question.id)).status == "open"


async def test_a_second_tap_is_refused_and_makes_no_second_draft() -> None:
    setup = _Setup()
    _, question = await _open_question(setup)
    await answer_clarification(
        setup.user_id, question.id, ["gym"], timezone="Europe/Warsaw", services=setup.services
    )

    with pytest.raises(ClarificationResolvedError):
        await answer_clarification(
            setup.user_id,
            question.id,
            ["cycling"],
            timezone="Europe/Warsaw",
            services=setup.services,
        )

    assert [draft.sport_id for draft in setup.drafts.all_of(setup.user_id)] == ["gym"]


async def test_another_users_question_is_not_found_and_not_answered() -> None:
    setup = _Setup()
    _, question = await _open_question(setup)

    with pytest.raises(ClarificationNotFoundError):
        await answer_clarification(
            uuid4(), question.id, ["gym"], timezone="Europe/Warsaw", services=setup.services
        )

    assert (await setup.clarifications.get(setup.user_id, question.id)).status == "open"


async def test_a_failed_extraction_leaves_the_question_open_to_tap_again() -> None:
    setup = _Setup(conversation=ScriptedConversation(extract_outcomes=[LlmCallOutcome.TIMEOUT]))
    _, question = await _open_question(setup)

    failed = await answer_clarification(
        setup.user_id, question.id, ["gym"], timezone="Europe/Warsaw", services=setup.services
    )

    assert failed == ReplyFailed(outcome=LlmCallOutcome.TIMEOUT)
    assert (await setup.clarifications.get(setup.user_id, question.id)).status == "open"
    assert setup.drafts.all_of(setup.user_id) == []


async def test_let_me_type_instead_abandons_the_question_without_a_draft() -> None:
    setup = _Setup()
    _, question = await _open_question(setup)

    abandoned = await abandon_clarification(setup.user_id, question.id, services=setup.services)

    assert (abandoned.status, abandoned.answer_option_ids) == ("abandoned", None)
    assert setup.drafts.all_of(setup.user_id) == []
    with pytest.raises(ClarificationResolvedError):
        await abandon_clarification(setup.user_id, question.id, services=setup.services)


async def test_a_resumed_reply_in_a_new_context_sees_only_its_own_conversation() -> None:
    """The resumed run reads the history before the log, as the first run did."""
    setup = _Setup()
    conversation_id = await setup.chat.conversation_of(setup.user_id)
    earlier = await setup.chat.add(
        setup.user_id, conversation_id, ChatRole.USER, "morning", FROZEN_NOW - timedelta(hours=3)
    )
    _, question = await _open_question(setup)

    await answer_clarification(
        setup.user_id, question.id, ["cycling"], timezone="Europe/Warsaw", services=setup.services
    )

    [(context, _)] = setup.conversation.extracted
    assert [turn.text for turn in context.history] == [earlier.text]
    assert isinstance(context.user_id, UUID)


async def test_a_question_of_another_purpose_is_not_a_sport_answer() -> None:
    """Only the sport question is resumed here; others land with their own tickets."""
    setup = _Setup()
    message, question = await _open_question(setup)
    other = await setup.clarifications.add(
        setup.user_id,
        question.conversation_id,
        message.id,
        "same_session",
        question.clarification,
        created_at=FROZEN_NOW,
    )

    with pytest.raises(ClarificationNotFoundError):
        await answer_clarification(
            setup.user_id, other.id, ["gym"], timezone="Europe/Warsaw", services=setup.services
        )

    assert setup.conversation.extracted == []


async def test_a_draft_that_cannot_be_saved_leaves_the_question_open_to_tap_again() -> None:
    setup = _Setup()
    _, question = await _open_question(setup)

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("database down")

    setup.drafts.add = broken  # type: ignore[method-assign,assignment]

    with pytest.raises(RuntimeError):
        await answer_clarification(
            setup.user_id,
            question.id,
            ["gym"],
            timezone="Europe/Warsaw",
            services=setup.services,
        )

    reopened = await setup.clarifications.get(setup.user_id, question.id)
    assert (reopened.status, reopened.answer_option_ids) == ("open", None)


async def test_the_resumed_extraction_reads_the_log_as_of_when_it_was_sent() -> None:
    """A log sent at 00:10 and answered the next morning still dates "last night" from 00:10
    (ADR-0014), as a sport change and a retry do; same-session continuity hangs on that day
    (ticket #166)."""
    setup = _Setup()
    sent = FROZEN_NOW - timedelta(hours=9)
    conversation_id = await setup.chat.conversation_of(setup.user_id)
    message = await setup.chat.add(setup.user_id, conversation_id, ChatRole.USER, _INTERVALS, sent)
    events = await start_reply(
        setup.user_id, message.id, timezone="Europe/Warsaw", services=setup.services
    )
    [question] = _done([event async for event in events]).clarifications

    await answer_clarification(
        setup.user_id, question.id, ["cycling"], timezone="Europe/Warsaw", services=setup.services
    )

    [(context, sport)] = setup.conversation.extracted
    assert (sport, context.now) == ("cycling", sent)
