"""Asking whether a log is the same session (PRD-0004 B16, B17, ADR-0014, ADR-0015, ticket
#166): a same-sport log beyond the merge window, on its session's own day, is stored as a
pending draft extending nothing, with an open question on its card. The answer sets or clears
the session it extends; nothing else is written before it, and its confirm waits for it."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.drafts import confirm_draft
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.reply import (
    ReplyDone,
    SessionSettled,
    abandon_clarification,
    answer_clarification,
    change_draft_sport,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    NEW_SESSION_OPTION,
    ClarificationNotFoundError,
    ClarificationResolvedError,
    StoredClarification,
    UnknownOptionError,
)
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction
from ai_trainer.domain.sports.gym.extraction import ExtractedExercise, GymExtraction
from ai_trainer.domain.training import DraftAwaitingAnswerError, SessionSpan, StoredDraft
from tests.chat_support import (
    FROZEN_NOW,
    SAME_SESSION_QUESTION_TEXT,
    THREE_6A_TWO_6B,
    FixedClock,
    InMemoryChatRepository,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    InMemoryTrainingSessions,
    ScriptedConversation,
    reply_services,
)
from tests.feedback_support import InMemoryFeedbackEvents

_CLIMB = "I climbed 3 6A and 2 6B boulders"
# The morning's climbing session, its last log three hours before `FROZEN_NOW` (11:30 in
# Warsaw); its first log was at 08:00 there (CEST, UTC+2).
_MORNING = SessionSpan(
    id=uuid4(),
    occurred_on=date(2026, 10, 2),
    first_logged_at=FROZEN_NOW - timedelta(hours=3, minutes=30),
    last_logged_at=FROZEN_NOW - timedelta(hours=3),
)


class _Athlete:
    """A climber whose latest climbing session is `session`, logging through the reply."""

    def __init__(self, session: SessionSpan | None = _MORNING, *, owner: UUID | None = None):
        self.user_id = uuid4()
        self.chat = InMemoryChatRepository()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.sessions = InMemoryTrainingSessions()
        self.conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="climbing")]]
        )
        self.profile = InMemoryProfile({self.user_id: ["climbing", "gym"]})
        if session is not None:
            self.sessions.latest[(owner or self.user_id, "climbing")] = session
            self.drafts.spans[session.id] = session
        self.events = InMemoryFeedbackEvents()
        self.services = reply_services(
            self.chat,
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=self.sessions,
            profile=self.profile,
            conversation=self.conversation,
            feedback=FeedbackRecorder(self.events, clock=FixedClock()),
        )

    async def log(
        self,
        at: datetime = FROZEN_NOW,
        extraction: ClimbingExtraction = THREE_6A_TWO_6B,
    ) -> tuple[ChatMessage, ReplyDone]:
        self.conversation.extractions = [extraction]
        conversation_id = await self.chat.conversation_of(self.user_id)
        message = await self.chat.add(self.user_id, conversation_id, ChatRole.USER, _CLIMB, at)
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        *_, done = [event async for event in events]
        assert isinstance(done, ReplyDone)
        return message, done

    async def asked(self) -> tuple[StoredDraft, StoredClarification]:
        _, done = await self.log()
        [draft] = done.drafts
        [question] = done.clarifications
        return draft, question

    async def answer(self, question: StoredClarification, *option_ids: str) -> SessionSettled:
        settled = await answer_clarification(
            self.user_id,
            question.id,
            list(option_ids),
            timezone="Europe/Warsaw",
            services=self.services,
        )
        assert isinstance(settled, SessionSettled)
        return settled


async def test_a_log_three_hours_after_a_same_day_session_asks_same_or_new() -> None:
    athlete = _Athlete()

    message, done = await athlete.log()

    [draft] = done.drafts
    [question] = done.clarifications
    assert question.purpose == "same_session"
    assert question.status == "open"
    assert (question.message_id, question.draft_id) == (message.id, draft.id)
    assert question.clarification.question == SAME_SESSION_QUESTION_TEXT
    assert question.clarification.allow_free_text is False
    add, new = question.clarification.options
    assert (add.id, add.copy_key, add.params) == (
        str(_MORNING.id),
        "same_session_add",
        {"time": "08:00"},
    )
    assert (new.id, new.copy_key, new.params) == (NEW_SESSION_OPTION, "same_session_new", {})
    assert athlete.clarifications.all_of(athlete.user_id) == [question]


async def test_nothing_is_written_before_the_pick() -> None:
    athlete = _Athlete()

    draft, _ = await athlete.asked()

    assert draft.status == "pending"
    assert draft.session_id is None
    assert athlete.drafts.extends_ids[draft.id] is None
    assert athlete.drafts.confirmed_at == {}
    with pytest.raises(DraftAwaitingAnswerError):
        await confirm_draft(
            athlete.user_id, draft.id, drafts=athlete.drafts, clock=athlete.services.clock
        )
    assert athlete.drafts.confirmed_at == {}
    [stored] = athlete.drafts.all_of(athlete.user_id)
    assert stored.status == "pending"


async def test_picking_add_extends_the_earlier_session_and_then_confirms() -> None:
    athlete = _Athlete()
    draft, question = await athlete.asked()

    settled = await athlete.answer(question, str(_MORNING.id))

    assert settled.draft.extends == _MORNING
    assert athlete.drafts.extends_ids[draft.id] == _MORNING.id
    [answered] = athlete.clarifications.all_of(athlete.user_id)
    assert (answered.status, answered.answer_option_ids) == ("answered", (str(_MORNING.id),))
    confirmed = await confirm_draft(
        athlete.user_id, draft.id, drafts=athlete.drafts, clock=athlete.services.clock
    )
    assert confirmed.status == "confirmed"


async def test_picking_new_clears_the_session_it_would_extend() -> None:
    athlete = _Athlete()
    draft, question = await athlete.asked()

    settled = await athlete.answer(question, NEW_SESSION_OPTION)

    assert settled.draft.extends is None
    assert athlete.drafts.extends_ids[draft.id] is None
    [answered] = athlete.clarifications.all_of(athlete.user_id)
    assert (answered.status, answered.answer_option_ids) == ("answered", (NEW_SESSION_OPTION,))


@pytest.mark.parametrize(
    "option_ids",
    [["Add to the session at 08:00"], [str(uuid4())], [], ["new", "new"]],
    ids=["label-text", "another-session", "none", "two"],
)
async def test_an_option_the_question_did_not_offer_is_refused(option_ids: list[str]) -> None:
    athlete = _Athlete()
    draft, question = await athlete.asked()

    with pytest.raises(UnknownOptionError):
        await answer_clarification(
            athlete.user_id,
            question.id,
            option_ids,
            timezone="Europe/Warsaw",
            services=athlete.services,
        )

    [still] = athlete.clarifications.all_of(athlete.user_id)
    assert still.status == "open"
    assert athlete.drafts.extends_ids[draft.id] is None


async def test_an_answered_question_is_not_answered_again() -> None:
    athlete = _Athlete()
    draft, question = await athlete.asked()
    await athlete.answer(question, NEW_SESSION_OPTION)

    with pytest.raises(ClarificationResolvedError):
        await athlete.answer(question, str(_MORNING.id))

    assert athlete.drafts.extends_ids[draft.id] is None


async def test_another_users_question_is_not_found() -> None:
    athlete = _Athlete()
    _, question = await athlete.asked()

    with pytest.raises(ClarificationNotFoundError):
        await answer_clarification(
            uuid4(),
            question.id,
            [NEW_SESSION_OPTION],
            timezone="Europe/Warsaw",
            services=athlete.services,
        )


async def test_a_log_within_the_window_is_offered_as_an_addition_with_no_question() -> None:
    athlete = _Athlete()

    _, done = await athlete.log(at=_MORNING.last_logged_at + timedelta(minutes=10))

    [draft] = done.drafts
    assert done.clarifications == ()
    assert athlete.drafts.extends_ids[draft.id] == _MORNING.id


async def test_a_log_on_another_day_starts_a_new_session_with_no_question() -> None:
    athlete = _Athlete()
    tomorrow = THREE_6A_TWO_6B.model_copy(update={"occurred_on": date(2026, 10, 3)})

    _, done = await athlete.log(at=FROZEN_NOW + timedelta(days=1), extraction=tomorrow)

    assert done.clarifications == ()
    assert athlete.clarifications.all_of(athlete.user_id) == []


async def test_another_users_same_day_session_is_never_asked_about() -> None:
    athlete = _Athlete(owner=uuid4())

    _, done = await athlete.log()

    assert done.clarifications == ()


async def test_a_log_at_00_10_about_the_evening_before_asks_about_that_evenings_session() -> None:
    evening = SessionSpan(
        id=uuid4(),
        occurred_on=date(2026, 10, 2),
        first_logged_at=datetime(2026, 10, 2, 17, 0, tzinfo=UTC),
        last_logged_at=datetime(2026, 10, 2, 19, 30, tzinfo=UTC),
    )
    athlete = _Athlete(evening)
    last_night = THREE_6A_TWO_6B.model_copy(update={"date_source": "relative"})
    # 00:10 on 3 October in Warsaw.
    sent = datetime(2026, 10, 2, 22, 10, tzinfo=UTC)

    _, done = await athlete.log(at=sent, extraction=last_night)

    [question] = done.clarifications
    add, _ = question.clarification.options
    assert (add.id, add.params) == (str(evening.id), {"time": "19:00"})


async def test_changing_the_sport_drops_the_question_that_no_longer_applies() -> None:
    athlete = _Athlete()
    athlete.conversation.sport_extractions = {
        "gym": GymExtraction(
            occurred_on=date(2026, 10, 2),
            date_source="assumed_today",
            activities=[ExtractedExercise(name="Deadlift", sets=3, reps=5)],
        )
    }
    draft, _ = await athlete.asked()

    changed = await change_draft_sport(
        athlete.user_id, draft.id, "gym", timezone="Europe/Warsaw", services=athlete.services
    )

    assert isinstance(changed, StoredDraft)
    [dropped] = athlete.clarifications.all_of(athlete.user_id)
    assert dropped.status == "abandoned"
    assert (
        await confirm_draft(
            athlete.user_id, draft.id, drafts=athlete.drafts, clock=athlete.services.clock
        )
        is not None
    )


async def test_changing_to_a_sport_with_its_own_same_day_session_asks_about_that_one() -> None:
    athlete = _Athlete()
    lifted = SessionSpan(
        id=uuid4(),
        occurred_on=date(2026, 10, 2),
        first_logged_at=FROZEN_NOW - timedelta(hours=5),
        last_logged_at=FROZEN_NOW - timedelta(hours=4),
    )
    athlete.sessions.latest[(athlete.user_id, "gym")] = lifted
    athlete.conversation.sport_extractions = {
        "gym": GymExtraction(
            occurred_on=date(2026, 10, 2),
            date_source="assumed_today",
            activities=[ExtractedExercise(name="Deadlift", sets=3, reps=5)],
        )
    }
    draft, _ = await athlete.asked()

    await change_draft_sport(
        athlete.user_id, draft.id, "gym", timezone="Europe/Warsaw", services=athlete.services
    )

    dropped, asked = athlete.clarifications.all_of(athlete.user_id)
    assert dropped.status == "abandoned"
    assert (asked.status, asked.draft_id) == ("open", draft.id)
    assert asked.clarification.options[0].id == str(lifted.id)


async def test_a_question_with_no_type_instead_cannot_be_skipped() -> None:
    athlete = _Athlete()
    draft, question = await athlete.asked()

    with pytest.raises(ClarificationNotFoundError):
        await abandon_clarification(athlete.user_id, question.id, services=athlete.services)

    [still] = athlete.clarifications.all_of(athlete.user_id)
    assert still.status == "open"
    with pytest.raises(DraftAwaitingAnswerError):
        await confirm_draft(
            athlete.user_id, draft.id, drafts=athlete.drafts, clock=athlete.services.clock
        )


async def test_a_question_of_a_purpose_nothing_answers_is_not_found() -> None:
    athlete = _Athlete()
    _, question = await athlete.asked()
    unanswerable = await athlete.clarifications.add(
        athlete.user_id,
        question.conversation_id,
        question.message_id,
        "intent",
        question.clarification,
        created_at=FROZEN_NOW,
    )

    with pytest.raises(ClarificationNotFoundError):
        await athlete.answer(unanswerable, NEW_SESSION_OPTION)


async def test_a_same_session_question_with_no_draft_is_not_found() -> None:
    athlete = _Athlete()
    _, question = await athlete.asked()
    orphan = await athlete.clarifications.add(
        athlete.user_id,
        question.conversation_id,
        question.message_id,
        "same_session",
        question.clarification,
        created_at=FROZEN_NOW,
    )

    with pytest.raises(ClarificationNotFoundError):
        await athlete.answer(orphan, NEW_SESSION_OPTION)


class _NoSeparateQuestions(InMemoryClarificationRepository):
    """Refuses a same-session question stored on its own: it must come with its draft, in the
    draft's own transaction, so no draft is ever left without it (ticket #166)."""

    async def add(self, *args: object, **kwargs: object) -> StoredClarification:
        raise AssertionError("a same-session question stored apart from its draft")


@pytest.mark.parametrize("path", ["reply", "change_sport"])
async def test_the_question_is_stored_with_its_draft_never_apart(path: str) -> None:
    athlete = _Athlete()
    draft: StoredDraft | None = None
    if path == "change_sport":
        athlete.sessions.latest[(athlete.user_id, "gym")] = _MORNING.model_copy(
            update={"id": uuid4()}
        )
        athlete.conversation.sport_extractions = {
            "gym": GymExtraction(
                occurred_on=date(2026, 10, 2),
                date_source="assumed_today",
                activities=[ExtractedExercise(name="Deadlift", sets=3, reps=5)],
            )
        }
        draft, _ = await athlete.asked()
    separate = _NoSeparateQuestions()
    separate.rows = athlete.clarifications.rows
    athlete.drafts.questions = separate
    athlete.services = replace(athlete.services, clarifications=separate)

    if path == "reply":
        _, done = await athlete.log()
        [draft] = done.drafts
        [question] = done.clarifications
    else:
        assert draft is not None
        await change_draft_sport(
            athlete.user_id, draft.id, "gym", timezone="Europe/Warsaw", services=athlete.services
        )
        *_, question = separate.all_of(athlete.user_id)

    assert (question.status, question.draft_id) == ("open", draft.id)


async def test_answering_the_same_session_question_records_one_choice_answered() -> None:
    athlete = _Athlete()
    draft, question = await athlete.asked()

    await athlete.answer(question, NEW_SESSION_OPTION)

    [event] = athlete.events.events
    assert event.kind.value == "choice_answered"
    assert set(event.fields) == {"same_session"}
    assert (event.template_id, event.template_version) == (
        draft.template_id,
        draft.template_version,
    )
    assert draft.template_id == "climbing.extract"
