"""The exercise card's edges found in review (PRD-0010 F23, ADR-0031, ticket #402): a row that an
edit made stale goes, every spelling of an answered name reads alike, and the name form survives
any refusal but a bad name."""

from ai_trainer.domain.clarification import KEEP_NAME_OPTION
from tests.unit.test_chat_exercise_question import _BARBELL, _HTMX, _App, _flat


async def test_a_rename_drops_the_answer_row_it_made_stale() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()
    app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [str(_BARBELL.id)]},
        headers=_HTMX,
    )

    response = app.client.post(
        f"/drafts/{draft_id}/edit", data={"activities.0.name": "squat"}, headers=_HTMX
    )

    assert f"hx-swap-oob=\"delete:[data-choice-answer='{question_id}']\"" in response.text
    assert "data-choice-answer" not in _flat(app.client.get("/").text)


async def test_a_page_load_carries_no_row_delete() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    app.client.post(
        f"/clarifications/{question_id}/answer", data={"option_ids": [KEEP_NAME_OPTION]}
    )

    page = app.client.get("/").text

    assert "data-choice-answer" in page
    assert 'hx-swap-oob="delete' not in page


async def test_every_spelling_of_an_answered_name_is_not_a_usual_name() -> None:
    app = _App(
        "bench press", "Bench  Press", usual={"bench press": "bench", "Bench  Press": "bench"}
    )
    _, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/answer",
        data={"option_ids": [str(_BARBELL.id)]},
        headers=_HTMX,
    )

    assert "you usually log this as" not in _flat(response.text)


async def test_the_name_form_keeps_itself_on_any_refusal_but_a_bad_name() -> None:
    app = _App()
    _, question_id, _ = await app.card()

    field = _flat(app.client.get(f"/clarifications/{question_id}/name", headers=_HTMX).text)

    assert 'hx-status:4xx="swap:none"' in field
    assert 'hx-status:422="swap:outerHTML"' in field


async def test_a_nul_in_a_typed_name_is_refused() -> None:
    app = _App()
    _, question_id, _ = await app.card()

    response = app.client.post(
        f"/clarifications/{question_id}/name", data={"name": "a\x00b"}, headers=_HTMX
    )

    assert response.status_code == 422
    assert app.open_questions() == [question_id]


async def test_an_unreadable_payload_keeps_the_page_and_drops_its_rows() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    app.client.post(
        f"/clarifications/{question_id}/answer", data={"option_ids": [KEEP_NAME_OPTION]}
    )
    owner, stored = app.drafts.rows[0]
    broken = stored.draft.model_copy(update={"payload": {"version": "nonsense"}})
    app.drafts.rows[0] = (owner, stored.model_copy(update={"draft": broken}))

    page = app.client.get("/")

    assert page.status_code == 200
    assert "data-choice-answer" not in page.text
