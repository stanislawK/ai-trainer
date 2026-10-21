"""Asking how to read a log (PRD-0005 B3, B17, ADR-0015, ticket #164): a climbing log the model
can't read safely gets concrete options to pick from, never a guess. The extraction sees the
climbing kinds of the athlete's recent sessions, asking writes nothing but the question, and
the tap resumes an extraction that can only draft."""

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from ai_trainer.application.reply import (
    ClarificationAnswered,
    ReplyDone,
    ReplyEvent,
    answer_clarification,
    change_draft_sport,
    retry_draft,
    sports_to_switch_to,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    ChoiceOption,
    ClarificationNotFoundError,
    StoredClarification,
    UnknownOptionError,
)
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.climbing.extraction import (
    ClimbingExtraction,
    ClimbingQuestion,
    ExtractedClimb,
)
from ai_trainer.domain.sports.climbing.payload import ClimbingActivity, ClimbingPayload, Grade
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.training import DraftSportNotOfferedError
from tests.chat_support import (
    DRAFT_READY_TEXT,
    FROZEN_NOW,
    READING_QUESTION_TEXT,
    SPORT_QUESTION_TEXT,
    InMemoryChatRepository,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    InMemoryTrainingSessions,
    ScriptedConversation,
    ScriptedFastRouter,
    ScriptedSafetyScreen,
    pending_draft,
    reply_services,
)

_LOG = "Did 3 7a"
_BOULDER_OR_LEAD = ClimbingQuestion.model_validate(
    {"readings": [{"kind": "boulder", "grade": "7a"}, {"kind": "lead", "grade": "7a"}]}
)
_THREE_7A_BOULDERS = ClimbingExtraction(
    occurred_on=date(2026, 10, 2),
    date_source="assumed_today",
    activities=[ExtractedClimb(kind="boulder", count=3, grade="7a")],
)
_THREE_7A_LEADS = ClimbingExtraction(
    occurred_on=date(2026, 10, 2),
    date_source="assumed_today",
    activities=[ExtractedClimb(kind="lead", count=3, grade="7a")],
)


def _session(*kinds: str) -> dict[str, Any]:
    return {
        "sport": "climbing",
        "activities": [{"kind": kind, "count": 1, "grade": None} for kind in kinds],
    }


class _Setup:
    """A climber whose log Jev routes to climbing; the extraction answers with `extractions`,
    one per call."""

    def __init__(
        self,
        *extractions: object,
        recent: tuple[dict[str, Any], ...] = (_session("boulder"), _session("lead")),
        sports: tuple[str, ...] = ("climbing",),
        max_options: int = 5,
        fast: ScriptedFastRouter | None = None,
        safety: ScriptedSafetyScreen | None = None,
    ) -> None:
        self.chat = InMemoryChatRepository()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.sessions = InMemoryTrainingSessions()
        self.user_id = uuid4()
        self.sessions.payloads[(self.user_id, "climbing")] = list(recent)
        self.conversation = ScriptedConversation(
            extractions=list(extractions or (_BOULDER_OR_LEAD, _THREE_7A_BOULDERS))  # type: ignore[arg-type]
        )
        self.fast = fast or ScriptedFastRouter(
            intents=[Intent(kind="log_session", sport="climbing")]
        )
        self.safety = safety or ScriptedSafetyScreen()
        self.profile = InMemoryProfile({self.user_id: list(sports)})
        self.max_options = max_options

    @property
    def services(self):  # type: ignore[no-untyped-def]
        return reply_services(
            self.chat,
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=self.sessions,
            profile=self.profile,
            conversation=self.conversation,
            fast_router=self.fast,
            safety_screen=self.safety,
            max_clarification_options=self.max_options,
        )

    async def log(self, text: str = _LOG) -> tuple[ChatMessage, ReplyDone]:
        conversation_id = await self.chat.conversation_of(self.user_id)
        message = await self.chat.add(
            self.user_id, conversation_id, ChatRole.USER, text, FROZEN_NOW
        )
        return message, await self.reply_to(message)

    async def reply_to(self, message: ChatMessage) -> ReplyDone:
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        return _done([event async for event in events])

    async def answer(self, question: StoredClarification, option_ids: list[str]) -> object:
        return await answer_clarification(
            self.user_id, question.id, option_ids, timezone="Europe/Warsaw", services=self.services
        )


def _done(events: list[ReplyEvent]) -> ReplyDone:
    assert isinstance(events[-1], ReplyDone)
    return events[-1]


async def test_an_ambiguous_log_asks_boulders_font_7a_or_routes_french_7a() -> None:
    setup = _Setup()

    message, done = await setup.log()

    [reply] = done.messages
    assert (reply.text, reply.template_id, reply.template_version) == (
        READING_QUESTION_TEXT,
        "reading_question",
        1,
    )
    assert reply.sports == ("climbing",)
    [question] = done.clarifications
    assert (question.purpose, question.status, question.message_id) == (
        "reading",
        "open",
        message.id,
    )
    assert question.clarification.sport == "climbing"
    assert question.clarification.question == READING_QUESTION_TEXT
    assert question.clarification.options == (
        ChoiceOption(id="boulder", copy_key="climbing_reading_boulder", params={"grade": "7A"}),
        ChoiceOption(id="lead", copy_key="climbing_reading_lead", params={"grade": "7a"}),
    )
    assert question.clarification.allow_free_text


async def test_the_extraction_sees_the_kinds_of_the_athletes_recent_sessions() -> None:
    setup = _Setup(recent=(_session("lead"), _session("boulder", "lead")))

    await setup.log()

    [(recent, answers, may_ask)] = setup.conversation.extract_options
    assert (recent, answers, may_ask) == (("lead", "boulder"), (), True)


async def test_a_lead_only_athletes_log_drafts_a_lead_with_no_question() -> None:
    setup = _Setup(_THREE_7A_LEADS, recent=(_session("lead"), _session("lead")))

    _, done = await setup.log()

    assert done.clarifications == ()
    [draft] = done.drafts
    [activity] = ClimbingPayload.model_validate(draft.draft.payload).activities
    assert (activity.kind, activity.grade) == ("lead", Grade(original="7a", scale="french"))
    assert setup.conversation.extract_options == [(("lead",), (), True)]


async def test_asking_writes_nothing_but_the_question() -> None:
    setup = _Setup()

    _, done = await setup.log()

    assert done.drafts == ()
    assert setup.drafts.all_of(setup.user_id) == []
    assert len(setup.clarifications.all_of(setup.user_id)) == 1


async def test_more_readings_than_the_limit_are_cut_to_the_likeliest() -> None:
    three = ClimbingQuestion.model_validate(
        {
            "readings": [
                {"kind": "boulder", "grade": "6c"},
                {"kind": "lead", "grade": "6c"},
                {"kind": "top_rope", "grade": "6c"},
            ]
        }
    )
    setup = _Setup(three, max_options=2)

    _, done = await setup.log()

    [question] = done.clarifications
    assert [option.id for option in question.clarification.options] == ["boulder", "lead"]


async def test_tapping_boulders_drafts_the_boulders_and_answers_the_question() -> None:
    setup = _Setup()
    message, done = await setup.log()
    [question] = done.clarifications

    answered = await setup.answer(question, ["boulder"])

    assert isinstance(answered, ClarificationAnswered)
    assert (answered.clarification.status, answered.clarification.answer_option_ids) == (
        "answered",
        ("boulder",),
    )
    [intro] = answered.reply.messages
    assert (intro.text, intro.template_id) == (DRAFT_READY_TEXT, "climbing.extract")
    [draft] = answered.reply.drafts
    assert (draft.sport_id, draft.message_id, draft.status) == ("climbing", message.id, "pending")
    assert ClimbingPayload.model_validate(draft.draft.payload).activities == [
        ClimbingActivity(kind="boulder", count=3, grade=Grade(original="7a", scale="font"))
    ]
    resumed = setup.conversation.extract_options[-1]
    assert resumed == (("boulder", "lead"), ("boulder",), True)
    [_, (context, sport)] = setup.conversation.extracted
    assert (context.message, context.now, sport) == (_LOG, message.created_at, "climbing")


async def test_the_resumed_draft_sorts_after_the_question() -> None:
    setup = _Setup()
    _, done = await setup.log()
    [question] = done.clarifications

    await setup.answer(question, ["lead"])

    thread = setup.chat.all_of(setup.user_id)
    assert [m.text for m in thread] == [_LOG, READING_QUESTION_TEXT, DRAFT_READY_TEXT]


@pytest.mark.parametrize(
    "option_ids",
    [["top_rope"], ["climbing"], [], ["boulder", "lead"], ["Boulders"]],
    ids=["unoffered kind", "a sport", "none", "two in single mode", "label text"],
)
async def test_an_option_the_question_never_offered_is_rejected_and_writes_nothing(
    option_ids: list[str],
) -> None:
    setup = _Setup()
    _, done = await setup.log()
    [question] = done.clarifications

    with pytest.raises(UnknownOptionError):
        await setup.answer(question, option_ids)

    assert len(setup.conversation.extracted) == 1
    assert setup.drafts.all_of(setup.user_id) == []
    assert (await setup.clarifications.get(setup.user_id, question.id)).status == "open"


async def test_a_failed_resumed_extraction_leaves_the_question_open_to_tap_again() -> None:
    setup = _Setup()
    _, done = await setup.log()
    [question] = done.clarifications
    setup.conversation.extract_outcomes = [LlmCallOutcome.SUCCESS, LlmCallOutcome.TIMEOUT]

    await setup.answer(question, ["boulder"])

    assert (await setup.clarifications.get(setup.user_id, question.id)).status == "open"
    assert setup.drafts.all_of(setup.user_id) == []


async def test_a_reading_question_with_no_sport_is_not_found() -> None:
    setup = _Setup()
    message, done = await setup.log()
    [question] = done.clarifications
    sportless = await setup.clarifications.add(
        setup.user_id,
        question.conversation_id,
        message.id,
        "reading",
        question.clarification.model_copy(update={"sport": None}),
        created_at=FROZEN_NOW,
    )

    with pytest.raises(ClarificationNotFoundError):
        await setup.answer(sportless, ["boulder"])


async def test_a_reload_replays_the_question_and_never_asks_twice() -> None:
    setup = _Setup()
    message, first = await setup.log()

    again = await setup.reply_to(message)

    assert (again.messages, again.clarifications) == (first.messages, first.clarifications)
    assert len(setup.conversation.extracted) == 1


async def test_a_mixed_log_asks_about_its_climbing_and_still_drafts_its_gym() -> None:
    """The question suspends the climbing intent only (ADR-0015): the gym part still drafts."""
    setup = _Setup(
        fast=ScriptedFastRouter(
            intents=[
                Intent(kind="log_session", sport="climbing"),
                Intent(kind="log_session", sport="gym"),
            ]
        ),
        sports=("climbing", "gym"),
    )
    setup.conversation.sport_extractions = {
        "climbing": _BOULDER_OR_LEAD,
        "gym": GymExtraction.model_validate(
            {
                "occurred_on": "2026-10-02",
                "date_source": "assumed_today",
                "activities": [{"name": "Bench press", "sets": 5, "reps": 5}],
            }
        ),
    }

    _, done = await setup.log("Did 3 7a, then bench 5x5")

    assert [m.text for m in done.messages] == [READING_QUESTION_TEXT, DRAFT_READY_TEXT]
    [question] = done.clarifications
    assert question.clarification.sport == "climbing"
    assert [draft.sport_id for draft in done.drafts] == ["gym"]


async def test_a_sport_answered_as_climbing_can_then_ask_how_to_read_it() -> None:
    setup = _Setup(
        fast=ScriptedFastRouter(
            intents=None, plausible_sports=("climbing", "gym"), single_intent="log_session"
        ),
        sports=("climbing", "gym"),
    )
    _, done = await setup.log()
    [sport_question] = done.clarifications
    assert sport_question.clarification.question == SPORT_QUESTION_TEXT

    answered = await setup.answer(sport_question, ["climbing"])

    assert isinstance(answered, ClarificationAnswered)
    assert answered.reply.drafts == ()
    [reading] = answered.reply.clarifications
    assert (reading.purpose, reading.status, reading.clarification.sport) == (
        "reading",
        "open",
        "climbing",
    )
    assert [m.text for m in answered.reply.messages] == [READING_QUESTION_TEXT]


async def test_a_pain_mention_still_gets_its_reply_first_beside_the_question() -> None:
    """A question delays no safety reply (ADR-0008 invariant 4): it is a fixed line after it."""
    setup = _Setup(safety=ScriptedSafetyScreen(pain=True))

    _, done = await setup.log("Did 3 7a, my finger hurts")

    assert done.messages[-1].text == READING_QUESTION_TEXT
    assert len(done.messages) == 2
    assert len(setup.conversation.chitchatted) == 1
    [question] = done.clarifications
    assert question.purpose == "reading"


async def test_a_sport_change_to_climbing_can_only_draft() -> None:
    setup = _Setup(_THREE_7A_LEADS, sports=("gym", "climbing"))
    conversation_id = await setup.chat.conversation_of(setup.user_id)
    message = await setup.chat.add(setup.user_id, conversation_id, ChatRole.USER, _LOG, FROZEN_NOW)
    stored = await setup.drafts.add(
        setup.user_id,
        conversation_id,
        message.id,
        "gym",
        (await pending_draft(InMemoryDraftRepository(), setup.user_id)).draft,
        payload_version=1,
        created_at=FROZEN_NOW,
    )

    await change_draft_sport(
        setup.user_id, stored.id, "climbing", timezone="Europe/Warsaw", services=setup.services
    )

    [(recent, answers, may_ask)] = setup.conversation.extract_options
    assert (recent, answers, may_ask) == (("boulder", "lead"), (), False)


async def test_a_retried_climbing_draft_can_only_draft() -> None:
    setup = _Setup(
        None,
        _THREE_7A_LEADS,
        fast=ScriptedFastRouter(
            intents=[
                Intent(kind="log_session", sport="climbing"),
                Intent(kind="chitchat"),
            ]
        ),
    )
    setup.conversation.extract_outcomes = [LlmCallOutcome.TIMEOUT, LlmCallOutcome.SUCCESS]
    _, done = await setup.log("Did 3 7a, how are you?")
    failed = next(m for m in done.messages if m.template_id == "draft_failed")

    await retry_draft(setup.user_id, failed.id, timezone="Europe/Warsaw", services=setup.services)

    assert setup.conversation.extract_options[-1] == (("boulder", "lead"), (), False)


async def test_the_tapped_reading_holds_even_when_the_model_drafts_another_kind() -> None:
    """The pick is enforced in code (ADR-0015 invariant 4, skeptic finding at #164)."""
    setup = _Setup(_BOULDER_OR_LEAD, _THREE_7A_LEADS)
    _, done = await setup.log()
    [question] = done.clarifications

    answered = await setup.answer(question, ["boulder"])

    assert isinstance(answered, ClarificationAnswered)
    [draft] = answered.reply.drafts
    assert ClimbingPayload.model_validate(draft.draft.payload).activities == [
        ClimbingActivity(kind="boulder", count=3, grade=Grade(original="7a", scale="font"))
    ]


async def _mixed_log_with_open_climbing_question(setup: _Setup) -> tuple[ChatMessage, ReplyDone]:
    setup.fast = ScriptedFastRouter(
        intents=[
            Intent(kind="log_session", sport="climbing"),
            Intent(kind="log_session", sport="gym"),
        ]
    )
    setup.conversation.sport_extractions = {
        "climbing": _BOULDER_OR_LEAD,
        "gym": GymExtraction.model_validate(
            {
                "occurred_on": "2026-10-02",
                "date_source": "assumed_today",
                "activities": [{"name": "Squat", "sets": 5, "reps": 5}],
            }
        ),
    }
    return await setup.log("Did 3 7a and squats 5x5")


async def test_a_sport_whose_question_is_open_on_the_log_is_not_offered_to_switch_to() -> None:
    """Switching the gym card to climbing would guess past the open reading question and
    leave two climbing drafts after the tap (skeptic finding at #164)."""
    setup = _Setup(sports=("climbing", "gym"))
    _, done = await _mixed_log_with_open_climbing_question(setup)
    [gym] = done.drafts

    with pytest.raises(DraftSportNotOfferedError):
        await change_draft_sport(
            setup.user_id, gym.id, "climbing", timezone="Europe/Warsaw", services=setup.services
        )

    assert [draft.sport_id for draft in setup.drafts.all_of(setup.user_id)] == ["gym"]
    assert len(setup.conversation.extracted) == 2


def test_sports_to_switch_to_leaves_out_the_sports_still_asking() -> None:
    sports = reply_services(InMemoryChatRepository()).sports

    assert sports_to_switch_to(["climbing", "gym", "cycling"], "gym", sports) == (
        "climbing",
        "cycling",
    )
    assert sports_to_switch_to(
        ["climbing", "gym", "cycling"], "gym", sports, asking=("climbing",)
    ) == ("cycling",)


async def test_the_tap_reads_only_the_climb_it_asked_about() -> None:
    """ "5 6B boulders, then a 7a" asked about the 7a; a lead tap leaves the boulders be."""
    boulders_then_a_7a = ClimbingExtraction(
        occurred_on=date(2026, 10, 2),
        date_source="assumed_today",
        activities=[
            ExtractedClimb(kind="boulder", count=5, grade="6B"),
            ExtractedClimb(kind="boulder", count=1, grade="7a"),
        ],
    )
    setup = _Setup(_BOULDER_OR_LEAD, boulders_then_a_7a)
    _, done = await setup.log("5 6B boulders, then a 7a")
    [question] = done.clarifications

    answered = await setup.answer(question, ["lead"])

    assert isinstance(answered, ClarificationAnswered)
    [draft] = answered.reply.drafts
    assert ClimbingPayload.model_validate(draft.draft.payload).activities == [
        ClimbingActivity(kind="boulder", count=5, grade=Grade(original="6B", scale="font")),
        ClimbingActivity(kind="lead", count=1, grade=Grade(original="7a", scale="french")),
    ]
