"""Settling a gym draft's names (PRD-0010 B37, ADR-0031, ADR-0015, ticket #397): history answers
with no question, an unsettled name gets one question at a time on the draft's card, a tap only
changes the draft, and no model is called for any of it."""

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_trainer.application.drafts import CardQuestions, confirm_draft, edit_draft
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.reply import (
    ReplyDone,
    SessionSettled,
    abandon_clarification,
    answer_clarification,
    change_draft_sport,
    retry_draft,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    EXERCISE_PURPOSE,
    KEEP_NAME_OPTION,
    NEW_SESSION_OPTION,
    SAME_SESSION_PURPOSE,
    ChoiceOption,
    Clarification,
    ClarificationNotFoundError,
    ClarificationResolvedError,
    StoredClarification,
    UnknownOptionError,
)
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import MAX_EXERCISE_NAME, ExerciseCandidate
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import (
    DRAFT_FAILED_TEMPLATE_ID,
    DraftAwaitingAnswerError,
    DraftDiscardedError,
    SessionSpan,
    StoredDraft,
)
from tests.chat_support import (
    DRAFT_FAILED_TEXT,
    EXERCISE_QUESTION_TEXT,
    FROZEN_NOW,
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
from tests.picker_support import ScriptedPicker

_BARBELL = ExerciseCandidate(
    id=uuid4(), name="Barbell Bench Press", equipment="barbell", target="pectorals"
)
_DUMBBELL = ExerciseCandidate(id=uuid4(), name="Dumbbell Bench Press", equipment="dumbbell")
_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)


class _Names:
    """The athlete's aliases and the shortlist, scripted. Records every read."""

    def __init__(
        self,
        aliases: Mapping[str, UUID] | None = None,
        candidates: Sequence[ExerciseCandidate] = (_BARBELL, _DUMBBELL),
    ) -> None:
        self.aliases = aliases or {}
        self.candidates = candidates
        self.matched: list[tuple[str, ...]] = []
        self.shortlisted: list[tuple[str, str, float, int]] = []

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        assert sport_id == "gym"
        assert near == _LIMITS.near_match
        self.matched.append(tuple(names))
        return {name: self.aliases[name] for name in names if name in self.aliases}

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        assert sport_id == "gym"
        self.shortlisted.append((name, measure, floor, limit))
        return list(self.candidates)[:limit]


def _lift(name: str = "bench press", reps: int = 8) -> dict[str, object]:
    return {
        "name": name,
        "sets": 3,
        "reps": reps,
        "load": {"original": "60kg", "value": 60, "unit": "kg"},
    }


def _gym(*activities: dict[str, object]) -> GymExtraction:
    return GymExtraction.model_validate(
        {"occurred_on": "2026-10-02", "date_source": "assumed_today", "activities": activities}
    )


class _Athlete:
    """A gym athlete logging through the reply, with the real resolver over scripted names."""

    def __init__(self, names: _Names | None = None, picker: ScriptedPicker | None = None) -> None:
        self.user_id = uuid4()
        self.names = names or _Names()
        self.picker = picker or ScriptedPicker()
        self.events = InMemoryFeedbackEvents()
        self.chat = InMemoryChatRepository()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.sessions = InMemoryTrainingSessions()
        self.conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]]
        )
        self.services = reply_services(
            self.chat,
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=self.sessions,
            profile=InMemoryProfile({self.user_id: ["gym", "climbing"]}),
            conversation=self.conversation,
            settlers={
                "gym": ResolveExercises(
                    self.names,
                    self.picker,
                    question=self.conversation.exercise_question,
                    limits=_LIMITS,
                )
            },
            feedback=FeedbackRecorder(self.events, clock=FixedClock()),
        )

    async def log(self, *activities: dict[str, object]) -> tuple[StoredDraft, ReplyDone]:
        self.conversation.extractions = [_gym(*activities)]
        conversation_id = await self.chat.conversation_of(self.user_id)
        message = await self.chat.add(
            self.user_id, conversation_id, ChatRole.USER, "bench press 3x8 60kg", FROZEN_NOW
        )
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        *_, done = [event async for event in events]
        assert isinstance(done, ReplyDone)
        [draft] = done.drafts
        return draft, done

    def open_question(self) -> StoredClarification | None:
        asked = [row for row in self.clarifications.all_of(self.user_id) if row.status == "open"]
        assert len(asked) <= 1  # one at a time (ADR-0015 invariant 1)
        return asked[0] if asked else None

    async def answer(self, question: StoredClarification, *option_ids: str) -> StoredDraft:
        settled = await answer_clarification(
            self.user_id,
            question.id,
            list(option_ids),
            timezone="Europe/Warsaw",
            services=self.services,
        )
        assert isinstance(settled, SessionSettled)
        return settled.draft

    async def stored(self, draft: StoredDraft) -> GymPayload:
        return GymPayload.model_validate(
            (await self.drafts.get(self.user_id, draft.id)).draft.payload
        )


async def test_a_name_with_an_alias_gets_its_exercise_with_no_question() -> None:
    athlete = _Athlete(_Names({"bench press": _BARBELL.id}))

    draft, done = await athlete.log(_lift())

    assert done.clarifications == ()
    assert athlete.open_question() is None
    assert (await athlete.stored(draft)).activities[0].exercise_id == _BARBELL.id
    assert athlete.names.shortlisted == []


async def test_an_unsettled_name_asks_on_the_card_with_candidates_and_keep_my_name() -> None:
    athlete = _Athlete()

    draft, done = await athlete.log(_lift())

    [question] = done.clarifications
    assert question.purpose == EXERCISE_PURPOSE
    assert question.draft_id == draft.id
    asked = question.clarification
    assert asked.question == EXERCISE_QUESTION_TEXT
    assert asked.subject == "bench press"
    assert asked.allow_free_text
    assert [option.id for option in asked.options] == [
        str(_BARBELL.id),
        str(_DUMBBELL.id),
        KEEP_NAME_OPTION,
    ]
    assert asked.options[0].params == {
        "name": "Barbell Bench Press",
        "detail": "barbell, pectorals",
    }
    assert asked.options[1].params == {"name": "Dumbbell Bench Press", "detail": "dumbbell"}
    assert asked.options[2].params == {"name": "bench press"}


async def test_the_shortlist_reads_the_names_measure_and_the_limits() -> None:
    athlete = _Athlete()

    await athlete.log(_lift())

    # Read for Jev's pick and again for the card Jev left, each with the same arguments.
    assert set(athlete.names.shortlisted) == {("bench press", "load_reps", 0.25, 4)}


async def test_no_candidate_above_the_floor_offers_only_keep_my_name() -> None:
    athlete = _Athlete(_Names(candidates=()))

    _, done = await athlete.log(_lift("xyzzy"))

    [question] = done.clarifications
    assert [option.id for option in question.clarification.options] == [KEEP_NAME_OPTION]
    assert question.clarification.allow_free_text


async def test_resolving_a_log_makes_no_text_model_call() -> None:
    """The extraction is the log's only text-model call: the resolver's one other model, Jev's
    pick, is a decision call through its own port, and a tap or a confirm makes none (G14,
    ADR-0031)."""
    athlete = _Athlete()

    draft, done = await athlete.log(_lift(), _lift("row"))
    [first] = done.clarifications
    await athlete.answer(first, str(_BARBELL.id))
    second = athlete.open_question()
    assert second is not None
    await athlete.answer(second, KEEP_NAME_OPTION)
    await confirm_draft(
        athlete.user_id, draft.id, drafts=athlete.services.drafts, clock=FixedClock()
    )

    assert len(athlete.conversation.routed) == 1
    assert len(athlete.conversation.extracted) == 1
    assert athlete.conversation.chitchatted == []


async def test_picking_a_candidate_fills_every_entry_of_that_name_and_nothing_else() -> None:
    athlete = _Athlete()

    draft, done = await athlete.log(_lift(reps=10), _lift("Bench  Press", reps=8), _lift("row"))
    [question] = done.clarifications
    await athlete.answer(question, str(_BARBELL.id))

    pyramid_one, pyramid_two, row = (await athlete.stored(draft)).activities
    assert pyramid_one.exercise_id == _BARBELL.id
    assert pyramid_two.exercise_id == _BARBELL.id
    assert row.exercise_id is None


async def test_keep_my_name_only_flags_the_draft() -> None:
    athlete = _Athlete()

    draft, done = await athlete.log(_lift())
    [question] = done.clarifications
    await athlete.answer(question, KEEP_NAME_OPTION)

    [exercise] = (await athlete.stored(draft)).activities
    assert exercise.keep_name
    assert exercise.exercise_id is None


async def test_several_names_ask_one_at_a_time_and_confirm_waits_for_the_last() -> None:
    athlete = _Athlete()

    draft, done = await athlete.log(_lift(), _lift("row"), _lift("curl"))
    [first] = done.clarifications
    assert first.clarification.subject == "bench press"
    await athlete.answer(first, str(_BARBELL.id))

    second = athlete.open_question()
    assert second is not None
    assert second.clarification.subject == "row"
    with pytest.raises(DraftAwaitingAnswerError):
        await confirm_draft(
            athlete.user_id, draft.id, drafts=athlete.services.drafts, clock=FixedClock()
        )
    await athlete.answer(second, KEEP_NAME_OPTION)

    third = athlete.open_question()
    assert third is not None
    assert third.clarification.subject == "curl"
    await athlete.answer(third, str(_DUMBBELL.id))

    assert athlete.open_question() is None
    confirmed = await confirm_draft(
        athlete.user_id, draft.id, drafts=athlete.services.drafts, clock=FixedClock()
    )
    assert confirmed.status == "confirmed"


async def test_entries_of_one_name_ask_once() -> None:
    athlete = _Athlete()

    await athlete.log(_lift(reps=10), _lift(reps=8), _lift(reps=6))

    assert len(athlete.clarifications.all_of(athlete.user_id)) == 1


async def test_a_same_session_question_goes_first_and_the_name_follows_its_answer() -> None:
    athlete = _Athlete()
    morning = SessionSpan(
        id=uuid4(),
        occurred_on=THREE_6A_TWO_6B.occurred_on,
        first_logged_at=FROZEN_NOW.replace(hour=6),
        last_logged_at=FROZEN_NOW.replace(hour=7),
    )
    athlete.sessions.latest[(athlete.user_id, "gym")] = morning
    athlete.drafts.spans[morning.id] = morning

    _, done = await athlete.log(_lift())

    [question] = done.clarifications
    assert question.purpose == SAME_SESSION_PURPOSE
    await athlete.answer(question, NEW_SESSION_OPTION)
    follow_up = athlete.open_question()
    assert follow_up is not None
    assert follow_up.purpose == EXERCISE_PURPOSE


async def test_let_me_type_instead_writes_nothing_to_the_draft_and_moves_to_the_next_name() -> None:
    athlete = _Athlete()

    draft, done = await athlete.log(_lift(), _lift("row"))
    [first] = done.clarifications
    before = await athlete.stored(draft)

    settled = await abandon_clarification(athlete.user_id, first.id, services=athlete.services)

    assert isinstance(settled, SessionSettled)
    assert await athlete.stored(draft) == before
    second = athlete.open_question()
    assert second is not None
    assert second.clarification.subject == "row"

    await abandon_clarification(athlete.user_id, second.id, services=athlete.services)

    assert athlete.open_question() is None
    confirmed = await confirm_draft(
        athlete.user_id, draft.id, drafts=athlete.services.drafts, clock=FixedClock()
    )
    assert confirmed.status == "confirmed"


async def test_an_option_never_offered_is_refused_and_changes_nothing() -> None:
    athlete = _Athlete()
    draft, done = await athlete.log(_lift())
    [question] = done.clarifications
    before = await athlete.stored(draft)

    with pytest.raises(UnknownOptionError):
        await athlete.answer(question, str(uuid4()))

    assert await athlete.stored(draft) == before
    assert athlete.open_question() is not None


async def test_a_second_tap_is_refused() -> None:
    athlete = _Athlete()
    _, done = await athlete.log(_lift())
    [question] = done.clarifications
    await athlete.answer(question, str(_BARBELL.id))

    with pytest.raises(ClarificationResolvedError):
        await athlete.answer(question, KEEP_NAME_OPTION)


async def test_a_tap_on_a_discarded_draft_is_refused() -> None:
    athlete = _Athlete()
    draft, done = await athlete.log(_lift())
    [question] = done.clarifications
    await athlete.drafts.discard(athlete.user_id, draft.id, discarded_at=FROZEN_NOW)

    with pytest.raises(DraftDiscardedError):
        await athlete.answer(question, str(_BARBELL.id))


def test_one_option_needs_the_free_text_escape_as_its_second_route() -> None:
    """ADR-0015 invariant 2: free text is an escape hatch, and options are never down to one way
    out with nothing beside it."""
    only = ChoiceOption(id=KEEP_NAME_OPTION, copy_key="gym_exercise_keep")

    allowed = Clarification(question="Which?", options=(only,), pending="log_session")

    assert allowed.options == (only,)
    with pytest.raises(ValidationError):
        Clarification(
            question="Which?", options=(only,), pending="log_session", allow_free_text=False
        )


async def test_an_exercise_question_with_no_draft_or_subject_is_not_found() -> None:
    athlete = _Athlete()
    _, done = await athlete.log(_lift())
    [question] = done.clarifications
    malformed = question.clarification.model_copy(update={"subject": None})
    orphan = await athlete.clarifications.add(
        athlete.user_id,
        question.conversation_id,
        question.message_id,
        EXERCISE_PURPOSE,
        malformed,
        created_at=FROZEN_NOW,
        draft_id=question.draft_id,
    )

    with pytest.raises(ClarificationNotFoundError):
        await athlete.answer(orphan, KEEP_NAME_OPTION)


async def test_an_exercise_question_on_a_sport_that_settles_nothing_is_not_found() -> None:
    athlete = _Athlete()
    draft, done = await athlete.log(_lift())
    [question] = done.clarifications
    await athlete.drafts.change_sport(
        athlete.user_id,
        draft.id,
        "climbing",
        draft.draft,
        payload_version=1,
        extends_session_id=None,
    )
    reopened = await athlete.clarifications.add(
        athlete.user_id,
        question.conversation_id,
        question.message_id,
        EXERCISE_PURPOSE,
        question.clarification,
        created_at=FROZEN_NOW,
        draft_id=draft.id,
    )

    with pytest.raises(ClarificationNotFoundError):
        await athlete.answer(reopened, KEEP_NAME_OPTION)


async def test_a_name_past_the_alias_limit_stays_typed_only_with_no_question() -> None:
    athlete = _Athlete()

    draft, done = await athlete.log(_lift("x" * (MAX_EXERCISE_NAME + 1)))

    assert done.clarifications == ()
    assert (await athlete.stored(draft)).activities[0].exercise_id is None
    assert athlete.names.matched == []


async def test_a_name_at_the_alias_limit_still_asks() -> None:
    athlete = _Athlete()

    _, done = await athlete.log(_lift("x" * MAX_EXERCISE_NAME))

    assert len(done.clarifications) == 1


async def test_a_sport_without_a_settler_is_untouched() -> None:
    athlete = _Athlete()
    athlete.conversation.intents = [[Intent(kind="log_session", sport="climbing")]]
    athlete.conversation.extractions = [THREE_6A_TWO_6B]
    conversation_id = await athlete.chat.conversation_of(athlete.user_id)
    message = await athlete.chat.add(
        athlete.user_id, conversation_id, ChatRole.USER, "3 6A", FROZEN_NOW
    )

    events = await start_reply(
        athlete.user_id, message.id, timezone="Europe/Warsaw", services=athlete.services
    )
    *_, done = [event async for event in events]

    assert isinstance(done, ReplyDone)
    assert done.clarifications == ()
    assert athlete.names.matched == []


async def test_moving_a_climbing_draft_to_gym_settles_its_names_and_asks() -> None:
    athlete = _Athlete(_Names({"row": _DUMBBELL.id}))
    athlete.conversation.intents = [[Intent(kind="log_session", sport="climbing")]]
    athlete.conversation.extractions = [THREE_6A_TWO_6B]
    conversation_id = await athlete.chat.conversation_of(athlete.user_id)
    message: ChatMessage = await athlete.chat.add(
        athlete.user_id, conversation_id, ChatRole.USER, "row and bench", FROZEN_NOW
    )
    events = await start_reply(
        athlete.user_id, message.id, timezone="Europe/Warsaw", services=athlete.services
    )
    *_, done = [event async for event in events]
    assert isinstance(done, ReplyDone)
    [draft] = done.drafts
    athlete.conversation.extractions = [_gym(_lift("row"), _lift())]

    moved = await change_draft_sport(
        athlete.user_id, draft.id, "gym", timezone="Europe/Warsaw", services=athlete.services
    )

    assert isinstance(moved, StoredDraft)
    row, bench = GymPayload.model_validate(moved.draft.payload).activities
    assert row.exercise_id == _DUMBBELL.id
    assert bench.exercise_id is None
    question = athlete.open_question()
    assert question is not None
    assert question.clarification.subject == "bench press"


async def test_moving_a_gym_draft_away_drops_its_question() -> None:
    athlete = _Athlete()
    draft, _ = await athlete.log(_lift())
    athlete.conversation.extractions = [THREE_6A_TWO_6B]

    moved = await change_draft_sport(
        athlete.user_id, draft.id, "climbing", timezone="Europe/Warsaw", services=athlete.services
    )

    assert not isinstance(moved, Exception)
    assert athlete.open_question() is None


async def test_a_retried_draft_settles_its_names_too() -> None:
    athlete = _Athlete()
    athlete.conversation.extractions = [_gym(_lift())]
    conversation_id = await athlete.chat.conversation_of(athlete.user_id)
    message = await athlete.chat.add(
        athlete.user_id, conversation_id, ChatRole.USER, "bench", FROZEN_NOW
    )
    part = await athlete.chat.add(
        athlete.user_id,
        conversation_id,
        ChatRole.ASSISTANT,
        DRAFT_FAILED_TEXT,
        message.created_at,
        sports=("gym",),
        template_id=DRAFT_FAILED_TEMPLATE_ID,
        template_version=1,
    )

    await retry_draft(athlete.user_id, part.id, timezone="Europe/Warsaw", services=athlete.services)

    question = athlete.open_question()
    assert question is not None
    assert question.clarification.subject == "bench press"


def _names_edit(*names: str) -> dict[str, object]:
    return {"activities": [{"name": name} for name in names]}


async def _edit(athlete: _Athlete, draft: StoredDraft, *names: str) -> StoredDraft:
    return await edit_draft(
        athlete.user_id,
        draft.id,
        _names_edit(*names),  # type: ignore[arg-type]  # the form's own shape
        drafts=athlete.services.drafts,
        sports=default_sport_registry(),
        feedback=athlete.services.feedback,
        questions=CardQuestions(
            clarifications=athlete.clarifications,
            settlers=athlete.services.settlers,
            clock=FixedClock(),
        ),
    )


async def test_renaming_a_settled_exercise_clears_its_settlement() -> None:
    athlete = _Athlete(_Names({"bench press": _BARBELL.id}))
    draft, _ = await athlete.log(_lift())

    edited = await _edit(athlete, draft, "incline press")

    [exercise] = GymPayload.model_validate(edited.draft.payload).activities
    assert exercise.exercise_id is None
    assert not exercise.keep_name
    assert athlete.open_question() is None


async def test_an_edit_that_keeps_the_name_keeps_the_settlement() -> None:
    athlete = _Athlete(_Names({"bench press": _BARBELL.id}))
    draft, _ = await athlete.log(_lift())

    edited = await _edit(athlete, draft, "bench press")

    [exercise] = GymPayload.model_validate(edited.draft.payload).activities
    assert exercise.exercise_id == _BARBELL.id


async def test_renaming_away_the_asked_name_drops_the_question_so_confirm_can_run() -> None:
    athlete = _Athlete()
    draft, _ = await athlete.log(_lift())

    await _edit(athlete, draft, "incline press")

    assert athlete.open_question() is None
    confirmed = await confirm_draft(
        athlete.user_id, draft.id, drafts=athlete.services.drafts, clock=FixedClock()
    )
    assert confirmed.status == "confirmed"


async def test_an_edit_that_keeps_the_asked_name_keeps_the_question() -> None:
    athlete = _Athlete()
    draft, _ = await athlete.log(_lift(), _lift("row"))

    await _edit(athlete, draft, "bench press", "seated row")

    question = athlete.open_question()
    assert question is not None
    assert question.clarification.subject == "bench press"


async def test_an_edit_racing_a_tap_on_the_question_is_not_refused() -> None:
    """The edit read the question open, and the tap closed it before the edit dropped it."""
    athlete = _Athlete()
    draft, _ = await athlete.log(_lift())

    async def tapped_first(*_args: object, **_kwargs: object) -> StoredClarification:
        raise ClarificationResolvedError

    athlete.clarifications.resolve = tapped_first  # type: ignore[method-assign]

    edited = await _edit(athlete, draft, "incline press")

    assert edited.status == "pending"


async def test_renaming_away_the_asked_name_moves_the_card_on_to_the_next_waiting_name() -> None:
    athlete = _Athlete()
    draft, _ = await athlete.log(_lift(), _lift("row"))

    await _edit(athlete, draft, "incline press", "row")

    following = athlete.open_question()
    assert following is not None
    assert following.clarification.subject == "row"
    with pytest.raises(DraftAwaitingAnswerError):
        await confirm_draft(
            athlete.user_id, draft.id, drafts=athlete.services.drafts, clock=FixedClock()
        )


async def test_a_name_passed_over_before_an_edit_is_not_asked_again_after_it() -> None:
    athlete = _Athlete()
    draft, done = await athlete.log(_lift(), _lift("row"), _lift("curl"))
    [first] = done.clarifications
    await abandon_clarification(athlete.user_id, first.id, services=athlete.services)

    await _edit(athlete, draft, "bench press", "seated row", "curl")

    following = athlete.open_question()
    assert following is not None
    assert following.clarification.subject == "curl"


async def test_a_question_dropped_by_an_edit_is_asked_again_when_its_name_comes_back() -> None:
    """Only the athlete's own skip keeps a name from being asked again on the card."""
    athlete = _Athlete()
    draft, _ = await athlete.log(_lift(), _lift("row"))
    await _edit(athlete, draft, "incline press", "row")
    row_question = athlete.open_question()
    assert row_question is not None
    await _edit(athlete, draft, "bench press", "row")

    await athlete.answer(row_question, KEEP_NAME_OPTION)

    following = athlete.open_question()
    assert following is not None
    assert following.clarification.subject == "bench press"


async def test_a_same_session_question_first_reads_no_shortlist() -> None:
    athlete = _Athlete()
    morning = SessionSpan(
        id=uuid4(),
        occurred_on=THREE_6A_TWO_6B.occurred_on,
        first_logged_at=FROZEN_NOW.replace(hour=6),
        last_logged_at=FROZEN_NOW.replace(hour=7),
    )
    athlete.sessions.latest[(athlete.user_id, "gym")] = morning
    athlete.drafts.spans[morning.id] = morning

    await athlete.log(_lift())

    assert athlete.names.shortlisted == []
    assert athlete.picker.asked == []


# --- Jev's pick (ticket #399) --------------------------------------------------------------


async def test_a_confident_jev_pick_fills_the_exercise_and_asks_nothing() -> None:
    athlete = _Athlete(picker=ScriptedPicker({"bench press": _DUMBBELL}))

    draft, done = await athlete.log(_lift(reps=10), _lift("Bench  Press", reps=8), _lift("row"))

    [question] = done.clarifications  # only the row, which Jev did not pick
    assert question.clarification.subject == "row"
    first, second, row = (await athlete.stored(draft)).activities
    assert (first.exercise_id, second.exercise_id) == (_DUMBBELL.id, _DUMBBELL.id)
    assert first.jev_pick
    assert second.jev_pick
    assert row.exercise_id is None
    assert not row.jev_pick


async def test_a_confident_jev_pick_leaves_no_card_at_all() -> None:
    athlete = _Athlete(picker=ScriptedPicker({"bench press": _BARBELL}))

    draft, done = await athlete.log(_lift())

    assert done.clarifications == ()
    assert athlete.open_question() is None
    assert (await athlete.stored(draft)).activities[0].exercise_id == _BARBELL.id


async def test_jev_is_asked_once_per_name_with_the_shortlist_it_was_given() -> None:
    athlete = _Athlete()

    await athlete.log(_lift(reps=10), _lift("Bench  Press", reps=8), _lift("row"))

    asked = {name: candidates for _, name, candidates in athlete.picker.asked}
    assert sorted(asked) == ["bench press", "row"]
    assert asked["bench press"] == (_BARBELL, _DUMBBELL)
    assert {user_id for user_id, _, _ in athlete.picker.asked} == {athlete.user_id}


async def test_a_pick_jev_declined_leaves_the_card_with_its_candidates() -> None:
    athlete = _Athlete(picker=ScriptedPicker())

    draft, done = await athlete.log(_lift())

    [question] = done.clarifications
    assert [option.id for option in question.clarification.options] == [
        str(_BARBELL.id),
        str(_DUMBBELL.id),
        KEEP_NAME_OPTION,
    ]
    [exercise] = (await athlete.stored(draft)).activities
    assert exercise.exercise_id is None
    assert not exercise.jev_pick


async def test_an_empty_shortlist_skips_jev() -> None:
    athlete = _Athlete(_Names(candidates=()))

    await athlete.log(_lift("xyzzy"))

    assert athlete.picker.asked == []


async def test_a_name_the_athletes_history_knows_is_never_put_to_jev() -> None:
    athlete = _Athlete(_Names({"bench press": _BARBELL.id}))

    await athlete.log(_lift())

    assert athlete.picker.asked == []


async def test_an_exercise_picked_by_jev_makes_a_row_on_no_alias_before_the_confirm() -> None:
    """The picker and the resolver write nothing: the alias is the confirm's (invariant 1)."""
    athlete = _Athlete(picker=ScriptedPicker({"bench press": _BARBELL}))

    await athlete.log(_lift())

    assert athlete.names.aliases == {}


async def test_renaming_an_exercise_jev_picked_is_a_decision_overridden_event() -> None:
    athlete = _Athlete(picker=ScriptedPicker({"bench press": _BARBELL}))
    draft, _ = await athlete.log(_lift())

    edited = await _edit(athlete, draft, "incline press")

    [exercise] = GymPayload.model_validate(edited.draft.payload).activities
    assert exercise.exercise_id is None
    assert not exercise.jev_pick
    [event] = [e for e in athlete.events.events if e.kind is FeedbackKind.DECISION_OVERRIDDEN]
    assert (event.template_id, event.template_version) == ("exercise_shortlist", 1)
    assert list(event.fields) == ["exercise_id"]
    assert event.user_id == athlete.user_id


async def test_an_edit_that_keeps_a_jev_picked_name_is_no_override() -> None:
    athlete = _Athlete(picker=ScriptedPicker({"bench press": _BARBELL}))
    draft, _ = await athlete.log(_lift())

    edited = await _edit(athlete, draft, "bench press")

    [exercise] = GymPayload.model_validate(edited.draft.payload).activities
    assert exercise.exercise_id == _BARBELL.id
    assert exercise.jev_pick
    assert [e for e in athlete.events.events if e.kind is FeedbackKind.DECISION_OVERRIDDEN] == []


async def test_renaming_a_name_the_history_settled_is_no_override() -> None:
    athlete = _Athlete(_Names({"bench press": _BARBELL.id}))
    draft, _ = await athlete.log(_lift())

    await _edit(athlete, draft, "incline press")

    assert [e for e in athlete.events.events if e.kind is FeedbackKind.DECISION_OVERRIDDEN] == []


async def test_a_jev_pick_is_not_asked_before_a_same_session_question_is_answered() -> None:
    athlete = _Athlete(picker=ScriptedPicker({"bench press": _BARBELL}))
    morning = SessionSpan(
        id=uuid4(),
        occurred_on=THREE_6A_TWO_6B.occurred_on,
        first_logged_at=FROZEN_NOW.replace(hour=6),
        last_logged_at=FROZEN_NOW.replace(hour=7),
    )
    athlete.sessions.latest[(athlete.user_id, "gym")] = morning
    athlete.drafts.spans[morning.id] = morning

    draft, done = await athlete.log(_lift())

    [question] = done.clarifications
    assert question.purpose == SAME_SESSION_PURPOSE
    assert athlete.picker.asked == []
    assert (await athlete.stored(draft)).activities[0].exercise_id is None
