"""The exercise card (PRD-0010 B37, F23, ADR-0031, ADR-0015, tickets #397 and #402): an unsettled
name asks on its own card, in place of the draft's, with its candidates, "Keep my name" and "Let me
type instead". A tap leaves a row above the card that follows; the name field keeps a typed name."""

import re
from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.clarification import KEEP_NAME_OPTION
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import MAX_EXERCISE_NAME, ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.payload import GymPayload
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
from tests.picker_support import ScriptedPicker

_HTMX = {"HX-Request": "true"}
_HOW = "Lie on the bench and lower the bar to your chest, then press it back up."
_BARBELL = ExerciseCandidate(
    id=uuid4(), name="Barbell Bench Press", equipment="barbell", target="pectorals", how=_HOW
)


class _Names:
    def __init__(
        self,
        candidates: Sequence[ExerciseCandidate],
        *,
        matched: Mapping[str, UUID] | None = None,
        usual: Mapping[str, str] | None = None,
    ) -> None:
        self.candidates = candidates
        self.matched = dict(matched or {})
        self.usual = dict(usual or {})

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> dict[str, UUID]:
        return {name: self.matched[name] for name in names if name in self.matched}

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> dict[str, ExerciseLabel]:
        return {
            typed: ExerciseLabel(name=_BARBELL.name, usual=self.usual.get(typed)) for typed in named
        }

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
    def __init__(
        self,
        *names: str,
        candidates: Sequence[ExerciseCandidate] = (_BARBELL,),
        matched: Mapping[str, UUID] | None = None,
        usual: Mapping[str, str] | None = None,
    ) -> None:
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
                    _Names(candidates, matched=matched, usual=usual),
                    ScriptedPicker(),
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

    async def reply(self) -> str:
        message = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            "bench press 3x8",
            FROZEN_NOW,
        )
        return self.client.get(f"/messages/{message.id}/reply").text

    async def card(self) -> tuple[UUID, UUID, str]:
        body = await self.reply()
        [draft] = self.drafts.all_of(self.user.id)
        [question] = self.clarifications.all_of(self.user.id)
        return draft.id, question.id, body

    def payload(self) -> GymPayload:
        [draft] = self.drafts.all_of(self.user.id)
        return GymPayload.model_validate(draft.draft.payload)

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
    assert "Which exercise is “bench press”?" in html
    assert "Barbell Bench Press" in html
    assert f"""hx-vals='{{"option_ids": "{_BARBELL.id}"}}'""" in html
    assert f"""hx-vals='{{"option_ids": "{KEEP_NAME_OPTION}"}}'""" in html
    assert "Keep my name" in html
    assert "Log it as you wrote it" in html
    assert "Let me type instead" in html
    assert f'hx-get="/clarifications/{question_id}/name"' in html
    # The question takes the draft's place: nothing to confirm until it is answered.
    assert f"/drafts/{draft_id}/confirm" not in html


async def test_exercise_card_candidates_show_catalogue_how_to() -> None:
    app = _App()

    _, _, body = await app.card()

    html = _flat(body)
    assert f'<span class="text-[13px] leading-[18px] lbl-2 line-clamp-2">{_HOW}</span>' in html
    assert "barbell, pectorals" not in html


async def test_a_candidate_with_no_catalogue_text_shows_no_how_to_line() -> None:
    app = _App(candidates=(ExerciseCandidate(id=uuid4(), name="My Own Press"),))

    _, _, body = await app.card()

    html = _flat(body)
    assert "My Own Press" in html
    assert "line-clamp-2" not in html


async def test_exercise_card_has_no_media_slot() -> None:
    app = _App()

    _, _, body = await app.card()

    html = _flat(body)
    assert "size-16" not in html
    assert "<img" not in html


async def test_card_with_no_candidate_shows_keep_and_type_instead() -> None:
    app = _App(candidates=())

    _, _, body = await app.card()

    html = _flat(body)
    assert "I don't have “bench press” in my list." in html
    assert "Which exercise is" not in html
    assert html.count("data-choice-option=") == 1
    assert "Keep my name" in html
    assert "Let me type instead" in html


async def test_let_me_type_opens_the_name_field_and_back_returns() -> None:
    app = _App()
    _, question_id, _ = await app.card()

    field = _flat(app.client.get(f"/clarifications/{question_id}/name", headers=_HTMX).text)

    assert "What should this exercise be called?" in field
    assert 'value="bench press"' in field
    assert "Use this name" in field
    assert app.open_questions() == [question_id]

    back = _flat(app.client.get(f"/clarifications/{question_id}/card", headers=_HTMX).text)

    assert "Which exercise is “bench press”?" in back
    assert "Use this name" not in back
    assert app.open_questions() == [question_id]


async def test_use_this_name_renames_and_keeps_the_name() -> None:
    app = _App("bench press", "bench  press")
    draft_id, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/name", data={"name": "  floor press "}, headers=_HTMX
    )

    assert response.status_code == 200
    html = _flat(response.text)
    assert "Kept your name" in html
    assert html.index("Kept your name") < html.index(f'data-draft-id="{draft_id}"')
    assert "you wrote" not in html
    assert " disabled" not in _confirm(html)
    assert [entry.name_as_typed for entry in app.payload().activities] == ["floor press"] * 2
    assert all(entry.keep_name for entry in app.payload().activities)
    assert app.open_questions() == []


async def test_a_typed_name_shown_on_another_entry_settles_both() -> None:
    app = _App("bench press", "seated row")
    await app.reply()
    [question_id] = app.open_questions()

    response = app.client.post(
        f"/clarifications/{question_id}/name", data={"name": "Seated  Row"}, headers=_HTMX
    )

    assert "data-choice-option" not in response.text
    assert app.open_questions() == []
    assert all(entry.keep_name for entry in app.payload().activities)


async def test_the_kept_row_shows_the_typed_name_after_a_reload() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    app.client.post(f"/clarifications/{question_id}/name", data={"name": "floor press"})

    page = _flat(app.client.get("/").text)

    [row] = re.findall(r"data-choice-answer.*?</div></div>", page)
    assert "Kept your name" in row
    assert "floor press" in row
    assert "bench press" not in row


@pytest.mark.parametrize("name", ["   ", "x" * (MAX_EXERCISE_NAME + 1)], ids=["blank", "too-long"])
async def test_use_this_name_refuses_blank_and_too_long(name: str) -> None:
    app = _App()
    _, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/name", data={"name": name}, headers=_HTMX
    )

    assert response.status_code == 422
    html = _flat(response.text)
    assert "Use this name" in html
    assert 'aria-invalid="true"' in html
    assert app.open_questions() == [question_id]
    assert [entry.name_as_typed for entry in app.payload().activities] == ["bench press"]


async def test_the_name_routes_on_a_resolved_question_are_a_409() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    app.client.post(f"/clarifications/{question_id}/name", data={"name": "floor press"})

    for response in (
        app.client.get(f"/clarifications/{question_id}/name", headers=_HTMX),
        app.client.get(f"/clarifications/{question_id}/card", headers=_HTMX),
        app.client.post(f"/clarifications/{question_id}/name", data={"name": "again"}),
    ):
        assert response.status_code == 409


async def test_the_name_routes_on_another_users_question_are_a_404() -> None:
    owner = _App()
    _, question_id, _ = await owner.card()
    # Their question isn't in the other athlete's repository, as a user-scoped read isn't.
    other = _App()

    for response in (
        other.client.get(f"/clarifications/{question_id}/name", headers=_HTMX),
        other.client.get(f"/clarifications/{question_id}/card", headers=_HTMX),
        other.client.post(f"/clarifications/{question_id}/name", data={"name": "mine"}),
    ):
        assert response.status_code == 404


async def test_a_picked_candidate_leaves_a_you_wrote_row_above_the_draft() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [str(_BARBELL.id)]},
        headers=_HTMX,
    )

    html = _flat(response.text)
    assert html.index("you wrote: bench press") < html.index(f'data-draft-id="{draft_id}"')
    assert html.index("Barbell Bench Press") < html.index(f'data-draft-id="{draft_id}"')


async def test_gym_draft_shows_you_wrote_under_the_canonical_name() -> None:
    app = _App("bench", matched={"bench": _BARBELL.id})

    body = _flat(await app.reply())

    assert '<span class="text-[12px] lbl-2">you wrote: bench</span>' in body
    assert body.index("Barbell Bench Press") < body.index("you wrote: bench")


async def test_gym_draft_shows_you_usually_log_this_as_for_a_near_match() -> None:
    app = _App(
        "side raisez", matched={"side raisez": _BARBELL.id}, usual={"side raisez": "side raises"}
    )

    body = _flat(await app.reply())

    assert "you usually log this as side raises" in body
    assert "you wrote" not in body


async def test_a_name_answered_on_the_draft_is_never_a_usual_name() -> None:
    app = _App(usual={"bench press": "bench"})
    draft_id, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [str(_BARBELL.id)]},
        headers=_HTMX,
    )

    html = _flat(response.text)
    assert "you usually log this as" not in html
    assert f'data-draft-id="{draft_id}"' in html


async def test_kept_name_has_no_you_wrote_line() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [KEEP_NAME_OPTION]},
        headers=_HTMX,
    )

    html = _flat(response.text)
    assert "Kept your name" in html
    assert "you wrote" not in html
    assert f'data-draft-id="{draft_id}"' in html


async def test_a_typed_name_equal_to_the_canonical_name_has_no_you_wrote_line() -> None:
    app = _App("barbell  bench press", matched={"barbell  bench press": _BARBELL.id})

    body = _flat(await app.reply())

    assert "Barbell Bench Press" in body
    assert "you wrote" not in body


async def test_the_logged_card_shows_the_canonical_name_without_the_name_line() -> None:
    app = _App("bench", matched={"bench": _BARBELL.id})
    await app.reply()
    [draft] = app.drafts.all_of(app.user.id)

    html = _flat(app.client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX).text)

    assert "Barbell Bench Press" in html
    assert "you wrote" not in html


async def test_a_name_with_markup_is_escaped_on_the_card() -> None:
    app = _App("<b>press</b>", candidates=())

    _, _, body = await app.card()

    assert "<b>press</b>" not in body
    assert "&lt;b&gt;press&lt;/b&gt;" in body


async def test_the_question_stays_on_the_card_after_a_reload() -> None:
    app = _App()
    await app.card()

    page = _flat(app.client.get("/").text)

    assert "Which exercise is “bench press”?" in page
    assert "/confirm" not in page


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
    assert "Which exercise is “seated row”?" in html
    assert "data-choice-option" in html
    assert html.index("Kept your name") < html.index("data-choice-option")
    assert "/confirm" not in html


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


async def test_use_this_name_on_a_discarded_draft_is_a_409() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()
    app.client.post(f"/drafts/{draft_id}/discard", headers=_HTMX)

    response = app.client.post(f"/clarifications/{question_id}/name", data={"name": "mine"})

    assert response.status_code == 409


async def test_use_this_name_on_a_draft_gone_is_a_404() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    app.drafts.rows.clear()

    response = app.client.post(f"/clarifications/{question_id}/name", data={"name": "mine"})

    assert response.status_code == 404


async def test_the_name_field_is_only_for_an_exercise_question() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    owner, row = app.clarifications.rows[0]
    app.clarifications.rows[0] = (owner, row.model_copy(update={"purpose": "same_session"}))

    assert app.client.get(f"/clarifications/{question_id}/name").status_code == 404


async def test_the_draft_line_is_left_out_while_the_exercise_card_asks_and_after() -> None:
    line = '<p class="text-[16px] leading-6 pl-9'
    settled = _App("bench", matched={"bench": _BARBELL.id})
    assert line in _flat(await settled.reply())

    app = _App()
    _, question_id, body = await app.card()
    assert line not in _flat(body)

    app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [KEEP_NAME_OPTION]},
        headers=_HTMX,
    )
    assert line not in _flat(app.client.get("/").text)
