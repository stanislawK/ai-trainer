"""The beginner how-to on the exercise card (F23, ADR-0031 *Beginner how-to*, #592; #590's
`card-simple-*` frames): the card renders at once with each candidate's clamped original; a
catalogue candidate with no short version loads it from a user-scoped route that writes it and
returns the line, behind a skeleton; a stored one shows straight away with its AI mark and no
request; any fallback keeps the original with no error; a custom exercise or a `rejected` row
makes no request. "Looks wrong" (#593) flags a shown text for every athlete, records one
content-free `howto_flagged` event and leaves the question open. The writer and Jev's check are
fakes: no model is called."""

from collections.abc import Sequence
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.exercise_simple_howto import CardHowtos, WriteSimpleHowto
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercise_simple_texts import (
    HowtoCallFailed,
    HowtoCheck,
    HowtoSource,
    NewSimpleText,
    SimpleHowtoDraft,
    SimpleTextStatus,
    source_sha256,
)
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)
from tests.feedback_support import InMemoryFeedbackEvents
from tests.picker_support import ScriptedPicker
from tests.simple_howto_support import (
    CHECK_VERSION,
    NOW,
    FrozenClock,
    InMemorySimpleTexts,
    ScriptedCheck,
    ScriptedWriter,
    StoredRow,
)

_HTMX = {"HX-Request": "true"}
# The short line's own attributes, not the original block's selector that names it.
_SIMPLE = "data-howto data-simple"
_ORIGINAL = (
    "Stand with your feet shoulder-width apart, a dumbbell in each hand at your sides. Lift the"
    " dumbbells out to shoulder height, then lower them slowly."
)
_SHORT = "Hold a dumbbell in each hand. Lift them out to shoulder height, then lower slowly."
_LATERAL = ExerciseCandidate(id=uuid4(), name="Dumbbell Lateral Raise", how=_ORIGINAL)
_FRONT = ExerciseCandidate(id=uuid4(), name="Dumbbell Front Raise", how="Lift them forward.")
# An athlete's own exercise: the catalogue has no English text for it, so it gets no request.
_CUSTOM = ExerciseCandidate(id=uuid4(), name="My Raise", how="Raise it my way.")


def _source(candidate: ExerciseCandidate) -> HowtoSource:
    return HowtoSource(
        exercise_id=candidate.id,
        name=candidate.name,
        instructions=candidate.how or "",
        target="delts",
        equipment="dumbbell",
    )


def _draft(text: str = _SHORT) -> SimpleHowtoDraft:
    return SimpleHowtoDraft(
        text=text, template_id="exercise.simple_howto", template_version=1, model="test/model"
    )


class _Names:
    def __init__(self, candidates: Sequence[ExerciseCandidate]) -> None:
        self.candidates = candidates

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> dict[str, UUID]:
        return {}

    async def labels(
        self, user_id: UUID, *, named: object, near: float
    ) -> dict[str, ExerciseLabel]:
        return {}

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
        return self.candidates


def _extraction() -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [{"name": "lateral rises", "sets": 3, "reps": 12}],
        }
    )


class _App:
    def __init__(
        self,
        writer: ScriptedWriter | None = None,
        *,
        clarifications: InMemoryClarificationRepository | None = None,
        texts: InMemorySimpleTexts | None = None,
        csrf: bool = False,
    ) -> None:
        app = FastAPI()
        self.texts = texts or InMemorySimpleTexts(
            sources={candidate.id: _source(candidate) for candidate in (_LATERAL, _FRONT)}
        )
        self.feedback = InMemoryFeedbackEvents()
        self.writer = writer or ScriptedWriter(_draft())
        self.check = ScriptedCheck()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = clarifications or InMemoryClarificationRepository()
        self.profile = InMemoryProfile()
        conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]], extractions=[_extraction()]
        )
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=conversation,
            profile=self.profile,
            drafts=self.drafts,
            clarifications=self.clarifications,
            settlers={
                "gym": ResolveExercises(
                    _Names((_LATERAL, _FRONT, _CUSTOM)),
                    ScriptedPicker(),
                    question=conversation.exercise_question,
                    limits=ResolutionLimits(
                        near_match=0.75, candidate_floor=0.25, shortlist_size=4
                    ),
                )
            },
            card_howtos=CardHowtos(
                texts=self.texts,
                write=WriteSimpleHowto(
                    texts=self.texts,
                    writer=self.writer,
                    check=self.check,
                    clock=FrozenClock(),
                    max_chars=160,
                ),
                feedback=FeedbackRecorder(self.feedback, clock=FrozenClock()),
                clock=FrozenClock(),
            ),
        )
        if csrf:
            app.add_middleware(CsrfMiddleware, secret_key=b"test", templates=build_templates())
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = ["gym"]
        self.client = TestClient(app)

    async def reply(self) -> str:
        message = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            "lateral rises 3x12",
            FROZEN_NOW,
        )
        return " ".join(self.client.get(f"/messages/{message.id}/reply").text.split())

    def open_question(self) -> UUID:
        [question] = [
            row.id for row in self.clarifications.all_of(self.user.id) if row.status == "open"
        ]
        return question

    def load(self, candidate: ExerciseCandidate, question: UUID | None = None) -> str:
        response = self.client.post(
            f"/clarifications/{question or self.open_question()}/howto/{candidate.id}",
            headers=_HTMX,
        )
        assert response.status_code == 200
        return " ".join(response.text.split())

    def flag(self, candidate: ExerciseCandidate, question: UUID | None = None) -> str:
        response = self.client.post(
            f"/clarifications/{question or self.open_question()}/howto/{candidate.id}/flag",
            headers=_HTMX,
        )
        assert response.status_code == 200
        return " ".join(response.text.split())

    def store(self, candidate: ExerciseCandidate, status: SimpleTextStatus) -> None:
        self.texts.rows[candidate.id] = StoredRow(
            NewSimpleText(
                exercise_id=candidate.id,
                lang="en",
                text=_SHORT,
                template_id="exercise.simple_howto",
                template_version=1,
                model="test/model",
                source_sha256=source_sha256(candidate.how or ""),
                check_version=CHECK_VERSION,
                created_at=NOW,
            ),
            _SHORT,
            status,
        )


def _candidate(html: str, candidate: ExerciseCandidate) -> str:
    """The candidate's `group/cand` wrapper, up to the next candidate or "Keep my name"."""
    start = html.rindex("<", 0, html.index(f'data-candidate="{candidate.id}"'))
    following = html.find("data-choice-option=", html.index(f'data-choice-option="{candidate.id}"'))
    following = html.find("data-choice-option=", following + 1)
    end = html.rindex("<", 0, following) if following != -1 else len(html)
    return html[start:end]


def _loader_url(question: UUID, candidate: ExerciseCandidate) -> str:
    return f'hx-post="/clarifications/{question}/howto/{candidate.id}"'


# AC: the card renders before any text is written.
async def test_card_renders_the_original_with_a_loader_before_any_write() -> None:
    app = _App()

    html = await app.reply()

    block = _candidate(html, _LATERAL)
    assert _ORIGINAL in block
    assert _loader_url(app.open_question(), _LATERAL) in block
    assert 'hx-trigger="load"' in block
    # The skeleton waits for the request: two lines, hidden until it runs.
    assert block.count('class="skeleton') == 2
    assert 'aria-label="Writing a short version"' in block
    assert _SIMPLE not in block
    assert app.writer.calls == []
    assert app.texts.card_reads == [{_LATERAL.id, _FRONT.id, _CUSTOM.id}]


# AC: a first card writes the text and swaps it in; a second card shows it without a call.
async def test_first_card_writes_and_second_card_shows_it_without_a_call() -> None:
    app = _App()
    await app.reply()

    line = app.load(_LATERAL)

    assert _SIMPLE in line
    assert '<span class="sr-only">AI simplified: </span>' in line
    assert _SHORT in line
    assert "hx-trigger" not in line
    assert _ORIGINAL not in line
    assert [user for user, _ in app.writer.calls] == [app.user.id]

    page = " ".join(app.client.get("/").text.split())

    block = _candidate(page, _LATERAL)
    assert _SIMPLE in block
    assert _SHORT in block
    assert _loader_url(app.open_question(), _LATERAL) not in block
    # More shows the full original beside the short version (#581, #590).
    assert "Original instructions" in block
    assert _ORIGINAL in block
    assert "Short version simplified by AI" in block
    assert len(app.writer.calls) == 1


async def test_a_second_load_of_a_written_text_makes_no_call() -> None:
    app = _App()
    await app.reply()
    app.load(_LATERAL)

    line = app.load(_LATERAL)

    assert _SHORT in line
    assert len(app.writer.calls) == 1


# AC: a failed write leaves the original and no error.
async def test_failed_write_returns_the_original_without_an_error() -> None:
    app = _App(ScriptedWriter(HowtoCallFailed(reason="timeout")))
    await app.reply()

    line = app.load(_LATERAL)

    assert _ORIGINAL in line
    assert _SIMPLE not in line
    assert 'role="alert"' not in line
    assert "hx-trigger" not in line
    assert "skeleton" not in line
    assert app.texts.writes == []


async def test_a_text_failing_jev_leaves_the_original() -> None:
    app = _App()
    app.check.replies = [HowtoCheck(passed=False, version=CHECK_VERSION)]
    await app.reply()

    line = app.load(_LATERAL)

    assert _ORIGINAL in line
    assert _SIMPLE not in line


# AC: a custom exercise or a `rejected` row makes no request.
async def test_custom_exercise_and_rejected_row_render_no_loader() -> None:
    app = _App()
    app.store(_FRONT, SimpleTextStatus.REJECTED)

    html = await app.reply()

    question = app.open_question()
    for candidate in (_CUSTOM, _FRONT):
        block = _candidate(html, candidate)
        assert (candidate.how or "") in block
        assert _loader_url(question, candidate) not in block
        assert _SIMPLE not in block
        assert "Original instructions" not in block
    assert app.writer.calls == []


async def test_a_flagged_row_keeps_the_original_and_a_shown_one_needs_no_request() -> None:
    app = _App()
    app.store(_LATERAL, SimpleTextStatus.SHOWN)
    app.store(_FRONT, SimpleTextStatus.FLAGGED)

    html = await app.reply()

    question = app.open_question()
    shown = _candidate(html, _LATERAL)
    assert _SIMPLE in shown
    assert _loader_url(question, _LATERAL) not in shown
    flagged = _candidate(html, _FRONT)
    assert _SIMPLE not in flagged
    assert _loader_url(question, _FRONT) not in flagged
    assert _SHORT not in flagged


async def test_another_users_clarification_is_404() -> None:
    app = _App()
    await app.reply()
    question = app.open_question()
    stranger = _App(clarifications=app.clarifications)

    response = stranger.client.post(
        f"/clarifications/{question}/howto/{_LATERAL.id}", headers=_HTMX
    )

    assert response.status_code == 404
    assert stranger.writer.calls == []


async def test_option_not_on_the_question_is_404() -> None:
    app = _App()
    await app.reply()
    question = app.open_question()

    for option in (str(uuid4()), "keep", "not-an-id"):
        response = app.client.post(f"/clarifications/{question}/howto/{option}", headers=_HTMX)
        assert response.status_code == 404
    assert app.client.post(f"/clarifications/{uuid4()}/howto/{_LATERAL.id}").status_code == 404
    assert app.writer.calls == []


async def test_answered_question_is_409() -> None:
    app = _App()
    await app.reply()
    question = app.open_question()
    app.client.post(
        f"/clarifications/{question}/answer",
        data={"option_ids": str(_LATERAL.id)},
        headers=_HTMX,
    )

    response = app.client.post(f"/clarifications/{question}/howto/{_LATERAL.id}", headers=_HTMX)

    assert response.status_code == 409
    assert app.writer.calls == []


async def test_without_the_how_to_service_the_card_is_as_before() -> None:
    app = FastAPI()
    include_chat(app, build_templates())
    sign_in_as(
        app,
        User(
            id=uuid4(),
            sub="howto-off",
            email="off@example.com",
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
            created_at=FROZEN_NOW,
        ),
    )

    client = TestClient(app)
    response = client.post(f"/clarifications/{uuid4()}/howto/{uuid4()}", headers=_HTMX)
    flag = client.post(f"/clarifications/{uuid4()}/howto/{uuid4()}/flag", headers=_HTMX)

    assert (response.status_code, flag.status_code) == (404, 404)


_THANKS = "Thanks, the short version is hidden. Showing the original."


# AC (#593): after a flag, every athlete's next card shows the original.
async def test_flag_hides_the_short_version_for_every_athlete() -> None:
    app = _App()
    app.store(_LATERAL, SimpleTextStatus.SHOWN)
    await app.reply()

    answer = app.flag(_LATERAL)

    assert answer.startswith("<div")
    assert f'data-candidate="{_LATERAL.id}"' in answer
    assert _ORIGINAL in answer
    assert _THANKS in answer
    assert 'role="status"' in answer
    assert _SIMPLE not in answer
    assert _SHORT not in answer
    row = app.texts.rows[_LATERAL.id]
    assert (row.status, row.flagged_at) == (SimpleTextStatus.FLAGGED, NOW)

    other = _App(texts=app.texts)
    html = await other.reply()

    block = _candidate(html, _LATERAL)
    assert _ORIGINAL in block
    assert _SIMPLE not in block
    assert _SHORT not in block
    assert _loader_url(other.open_question(), _LATERAL) not in block
    assert "skeleton" not in block
    assert _THANKS not in block
    assert other.writer.calls == []


# AC (#593): the flag doesn't answer the question; tapping the row still picks it.
async def test_flag_leaves_the_question_open_and_the_row_still_answers() -> None:
    app = _App()
    app.store(_LATERAL, SimpleTextStatus.SHOWN)
    await app.reply()
    question = app.open_question()

    answer = app.flag(_LATERAL, question)

    assert [row.status for row in app.clarifications.all_of(app.user.id)] == ["open"]
    assert f'hx-post="/clarifications/{question}/answer"' in answer
    assert f'data-choice-option="{_LATERAL.id}"' in answer
    response = app.client.post(
        f"/clarifications/{question}/answer",
        data={"option_ids": str(_LATERAL.id)},
        headers=_HTMX,
    )
    assert response.status_code == 200
    assert [row.status for row in app.clarifications.all_of(app.user.id)] == ["answered"]


# AC (#593): one `howto_flagged` row, with no text or exercise name.
async def test_flag_writes_one_content_free_feedback_event() -> None:
    app = _App()
    app.store(_LATERAL, SimpleTextStatus.SHOWN)
    await app.reply()

    app.flag(_LATERAL)

    [event] = app.feedback.events
    assert event.user_id == app.user.id
    assert event.kind is FeedbackKind.HOWTO_FLAGGED
    assert (event.template_id, event.template_version) == ("exercise.simple_howto", 1)
    assert list(event.fields) == []
    assert event.created_at == NOW
    stored = str(event.model_dump())
    for content in (_SHORT, _ORIGINAL, _LATERAL.name, str(_LATERAL.id)):
        assert content not in stored


# AC (#593): an already flagged or missing text answers without error, with no second row.
async def test_flag_twice_or_with_no_text_is_200_and_records_once() -> None:
    app = _App()
    app.store(_LATERAL, SimpleTextStatus.SHOWN)
    await app.reply()
    question = app.open_question()
    app.flag(_LATERAL, question)

    again = app.flag(_LATERAL, question)
    missing = app.flag(_FRONT, question)
    custom = app.flag(_CUSTOM, question)
    app.store(_FRONT, SimpleTextStatus.REJECTED)
    rejected = app.flag(_FRONT, question)

    for answer, candidate in (
        (again, _LATERAL),
        (missing, _FRONT),
        (custom, _CUSTOM),
        (rejected, _FRONT),
    ):
        assert (candidate.how or "") in answer
        assert _THANKS in answer
        assert _SIMPLE not in answer
        assert 'role="alert"' not in answer
    assert len(app.feedback.events) == 1
    assert app.texts.rows[_FRONT.id].status is SimpleTextStatus.REJECTED


async def test_flag_another_users_question_is_404() -> None:
    app = _App()
    app.store(_LATERAL, SimpleTextStatus.SHOWN)
    await app.reply()
    question = app.open_question()
    stranger = _App(clarifications=app.clarifications, texts=app.texts)

    response = stranger.client.post(
        f"/clarifications/{question}/howto/{_LATERAL.id}/flag", headers=_HTMX
    )

    assert response.status_code == 404
    assert app.texts.rows[_LATERAL.id].status is SimpleTextStatus.SHOWN
    assert stranger.feedback.events == []


async def test_flag_option_not_on_the_question_is_404() -> None:
    app = _App()
    await app.reply()
    question = app.open_question()

    for option in (str(uuid4()), "keep", "not-an-id"):
        response = app.client.post(f"/clarifications/{question}/howto/{option}/flag", headers=_HTMX)
        assert response.status_code == 404
    unknown = app.client.post(f"/clarifications/{uuid4()}/howto/{_LATERAL.id}/flag")
    assert unknown.status_code == 404
    assert app.feedback.events == []


async def test_flag_answered_question_is_409() -> None:
    app = _App()
    app.store(_LATERAL, SimpleTextStatus.SHOWN)
    await app.reply()
    question = app.open_question()
    app.client.post(
        f"/clarifications/{question}/answer",
        data={"option_ids": str(_LATERAL.id)},
        headers=_HTMX,
    )

    response = app.client.post(
        f"/clarifications/{question}/howto/{_LATERAL.id}/flag", headers=_HTMX
    )

    assert response.status_code == 409
    assert app.texts.rows[_LATERAL.id].status is SimpleTextStatus.SHOWN
    assert app.feedback.events == []


async def test_flag_without_csrf_is_refused() -> None:
    app = _App(csrf=True)
    app.store(_LATERAL, SimpleTextStatus.SHOWN)

    response = app.client.post(f"/clarifications/{uuid4()}/howto/{_LATERAL.id}/flag", headers=_HTMX)

    assert response.status_code == 403
    assert app.texts.rows[_LATERAL.id].status is SimpleTextStatus.SHOWN
    assert app.feedback.events == []
