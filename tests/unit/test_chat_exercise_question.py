"""The exercise question on the draft card (PRD-0010 B37, F23, ADR-0031, ADR-0015, ticket #397): an
unsettled name asks with its candidates, "Keep my name" and "Let me type instead" in the card's
choice rows, Confirm waits for the answer, and a tap or a skip swaps in the card that follows."""

import re
from collections.abc import Sequence
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.clarification import KEEP_NAME_OPTION
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    EXERCISE_QUESTION_TEXT,
    FROZEN_NOW,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_BARBELL = ExerciseCandidate(
    id=uuid4(), name="Barbell Bench Press", equipment="barbell", target="pectorals"
)


class _Names:
    def __init__(self, candidates: Sequence[ExerciseCandidate]) -> None:
        self.candidates = candidates

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> dict[str, UUID]:
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


def _lift(name: str) -> dict[str, object]:
    return {"name": name, "sets": 3, "reps": 8}


class _App:
    def __init__(self, *names: str, candidates: Sequence[ExerciseCandidate] = (_BARBELL,)) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.profile = InMemoryProfile()
        conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]],
            extractions=[
                GymExtraction.model_validate(
                    {
                        "occurred_on": "2026-10-02",
                        "date_source": "assumed_today",
                        "activities": [_lift(name) for name in names or ("bench press",)],
                    }
                )
            ],
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
                    _Names(candidates),
                    question=conversation.exercise_question,
                    limits=ResolutionLimits(
                        near_match=0.75, candidate_floor=0.25, shortlist_size=4
                    ),
                )
            },
        )
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = ["gym"]
        self.client = TestClient(app)

    async def card(self) -> tuple[UUID, UUID, str]:
        message = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            "bench press 3x8",
            FROZEN_NOW,
        )
        body = self.client.get(f"/messages/{message.id}/reply").text
        [draft] = self.drafts.all_of(self.user.id)
        [question] = self.clarifications.all_of(self.user.id)
        return draft.id, question.id, body

    def open_questions(self) -> list[UUID]:
        return [row.id for row in self.clarifications.all_of(self.user.id) if row.status == "open"]


def _flat(html: str) -> str:
    return " ".join(html.split())


def _confirm(html: str) -> str:
    [button] = re.findall(r"<button[^>]*/confirm\"[^>]*>", html)
    return str(button)


async def test_the_card_asks_about_the_name_with_its_candidates_and_both_ways_out() -> None:
    app = _App()

    draft_id, question_id, body = await app.card()

    html = _flat(body)
    assert EXERCISE_QUESTION_TEXT in html
    assert "bench press" in html
    assert "Barbell Bench Press" in html
    assert "barbell, pectorals" in html
    assert f"""hx-vals='{{"option_ids": "{_BARBELL.id}"}}'""" in html
    assert f"""hx-vals='{{"option_ids": "{KEEP_NAME_OPTION}"}}'""" in html
    assert "Keep my name" in html
    assert "Let me type instead" in html
    assert f'hx-post="/clarifications/{question_id}/abandon"' in html
    assert " disabled" in _confirm(html)
    assert html.index("data-choice-option=") < html.index(f"/drafts/{draft_id}/confirm")


async def test_card_with_no_candidate_shows_keep_and_type_instead() -> None:
    app = _App(candidates=())

    _, _, body = await app.card()

    html = _flat(body)
    assert html.count("data-choice-option=") == 1
    assert "Keep my name" in html
    assert "Let me type instead" in html


async def test_a_name_with_markup_is_escaped_on_the_card() -> None:
    app = _App("<b>press</b>", candidates=())

    _, _, body = await app.card()

    assert "<b>press</b>" not in body
    assert "&lt;b&gt;press&lt;/b&gt;" in body


async def test_the_question_stays_on_the_card_after_a_reload() -> None:
    app = _App()
    await app.card()

    page = _flat(app.client.get("/").text)

    assert EXERCISE_QUESTION_TEXT in page
    assert " disabled" in _confirm(page)


async def test_picking_a_candidate_swaps_in_the_card_ready_to_confirm() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [str(_BARBELL.id)]},
        headers=_HTMX,
    )

    assert response.status_code == 200
    html = _flat(response.text)
    assert f'data-draft-id="{draft_id}"' in html
    assert "data-choice-option" not in html
    assert " disabled" not in _confirm(html)


async def test_a_tap_swaps_in_the_next_names_question() -> None:
    app = _App("bench press", "seated row")
    message = await app.chat.add(
        app.user.id,
        await app.chat.conversation_of(app.user.id),
        ChatRole.USER,
        "bench and row",
        FROZEN_NOW,
    )
    app.client.get(f"/messages/{message.id}/reply")
    [question_id] = app.open_questions()

    response = app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [KEEP_NAME_OPTION]},
        headers=_HTMX,
    )

    html = _flat(response.text)
    assert "seated row" in html
    assert "data-choice-option" in html
    assert " disabled" in _confirm(html)


async def test_let_me_type_instead_swaps_in_the_card_ready_to_confirm() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()

    response = app.client.post(f"/clarifications/{question_id}/abandon", headers=_HTMX)

    assert response.status_code == 200
    html = _flat(response.text)
    assert f'data-draft-id="{draft_id}"' in html
    assert "data-choice-option" not in html
    assert " disabled" not in _confirm(html)


async def test_a_skip_on_a_resolved_question_is_a_409() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    app.client.post(f"/clarifications/{question_id}/abandon", headers=_HTMX)

    response = app.client.post(f"/clarifications/{question_id}/abandon", headers=_HTMX)

    assert response.status_code == 409


async def test_a_skip_on_a_discarded_draft_is_a_409() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()
    app.client.post(f"/drafts/{draft_id}/discard", headers=_HTMX)

    response = app.client.post(f"/clarifications/{question_id}/abandon", headers=_HTMX)

    assert response.status_code == 409


async def test_an_option_the_question_never_offered_is_a_422() -> None:
    app = _App()
    _, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [str(uuid4())]},
        headers=_HTMX,
    )

    assert response.status_code == 422
    assert app.open_questions() == [question_id]


async def test_confirming_while_the_question_is_open_is_a_409() -> None:
    app = _App()
    draft_id, _, _ = await app.card()

    assert app.client.post(f"/drafts/{draft_id}/confirm", headers=_HTMX).status_code == 409


async def test_a_rename_on_the_edit_form_drops_the_stale_question() -> None:
    app = _App()
    draft_id, _, _ = await app.card()

    response = app.client.post(
        f"/drafts/{draft_id}/edit", data={"activities.0.name": "incline press"}, headers=_HTMX
    )

    assert response.status_code == 200
    html = _flat(response.text)
    assert "data-choice-option" not in html
    assert " disabled" not in _confirm(html)
    assert app.open_questions() == []
