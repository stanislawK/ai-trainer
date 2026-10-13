"""Editing a draft from its card (PRD-0004 F2, ADR-0014 invariant 4, ticket #158): Edit, or the
date chip, turns the card into an inline form; Save updates the pending draft and shows the
card again, a bad value is marked inline and changes nothing, Cancel shows the card as it is
stored, and a confirmed or discarded draft can't be edited."""

import re
from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.sports.climbing.payload import ClimbingPayload, Grade
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.drafts import form_changes
from ai_trainer.web.icons import render_icon
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    THREE_6A_TWO_6B,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_YESTERDAY = date(2026, 10, 1)


def _client() -> tuple[TestClient, UUID, InMemoryChatRepository, InMemoryDraftRepository]:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), conversation=ScriptedConversation(), drafts=drafts)
    user = sign_in_as(app)
    return TestClient(app), user.id, chat, drafts


async def _draft(
    chat: InMemoryChatRepository, drafts: InMemoryDraftRepository, user_id: UUID
) -> StoredDraft:
    """The athlete's "3 6A and 2 6B" log, its extraction reply, and the pending draft, dated
    today by assumption."""
    conversation_id = await chat.conversation_of(user_id)
    log = await chat.add(
        user_id, conversation_id, ChatRole.USER, "I climbed 3 6A and 2 6B boulders", FROZEN_NOW
    )
    await chat.add(
        user_id,
        conversation_id,
        ChatRole.ASSISTANT,
        "Here's the session.",
        FROZEN_NOW,
        sports=("climbing",),
        template_id="climbing.extract",
        template_version=1,
    )
    return await drafts.add(
        user_id,
        conversation_id,
        log.id,
        "climbing",
        SessionDraft(
            occurred_on=THREE_6A_TWO_6B.occurred_on,
            date_source=THREE_6A_TWO_6B.date_source,
            timezone="Europe/Warsaw",
            payload=THREE_6A_TWO_6B.to_payload().model_dump(mode="json"),
        ),
        payload_version=1,
        created_at=FROZEN_NOW,
    )


def _form(**fields: str) -> dict[str, str]:
    """The edit form for `THREE_6A_TWO_6B` as the browser posts it, with `fields` changed."""
    form = {
        "occurred_on": "2026-10-02",
        "duration_min": "",
        "rpe": "",
        "feeling": "",
        "activities.0.count": "3",
        "activities.0.grade": "6A",
        "activities.0.style": "",
        "activities.1.count": "2",
        "activities.1.grade": "6B",
        "activities.1.style": "",
    }
    form.update(fields)
    return form


def _card(html: str) -> str:
    card: str
    (card,) = re.findall(r'<article[^>]*aria-label="Draft session".*?</article>', html, re.S)
    return card


def _edit_form(html: str) -> str:
    form: str
    (form,) = re.findall(r'<article[^>]*aria-label="Edit draft session".*?</article>', html, re.S)
    return form


def _input(html: str, name: str) -> str:
    field: str
    (field,) = re.findall(rf'<(?:input|select)[^>]*name="{re.escape(name)}"[^>]*>', html)
    return field


def _button(html: str, label: str) -> str:
    button: str
    (button,) = re.findall(rf"<button[^>]*>(?:(?!</button>).)*{label}</button>", html, re.S)
    return button


async def test_edit_opens_the_cards_fields_as_an_inline_form() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    response = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX)

    assert response.status_code == 200
    form = _edit_form(response.text)
    assert f'hx-post="/drafts/{draft.id}/edit"' in form
    assert 'value="2026-10-02"' in _input(form, "occurred_on")
    assert 'type="date"' in _input(form, "occurred_on")
    for name in ("duration_min", "rpe", "feeling"):
        assert 'value=""' in _input(form, name)
    assert 'value="3"' in _input(form, "activities.0.count")
    assert 'value="6A"' in _input(form, "activities.0.grade")
    assert 'value="6B"' in _input(form, "activities.1.grade")
    assert "<select" in _input(form, "activities.1.style")
    assert 'aria-invalid="true"' not in form
    assert [d.draft for d in drafts.all_of(user_id)] == [draft.draft]


async def test_the_cards_edit_button_and_its_date_chip_both_open_the_form() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    card = _card(client.get("/", headers=_HTMX).text)

    edit = _button(card, "Edit")
    assert f'hx-get="/drafts/{draft.id}/edit"' in edit
    assert "disabled" not in edit
    (chip,) = re.findall(r"<button[^>]*data-date-source=[^>]*>.*?</button>", card, re.S)
    assert f'hx-get="/drafts/{draft.id}/edit"' in chip
    assert "Assumed: today" in chip
    assert render_icon("pencil", "size-3.5") in chip
    for opener in (edit, chip):
        assert 'hx-target="closest article"' in opener
        assert 'hx-swap="outerHTML"' in opener
        assert 'hx-status:4xx="swap:none"' in opener


async def test_changing_the_assumed_date_to_yesterday_then_confirming_saves_yesterday() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    saved = client.post(
        f"/drafts/{draft.id}/edit", data=_form(occurred_on="2026-10-01"), headers=_HTMX
    )
    confirmed = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert saved.status_code == 200
    card = _card(saved.text)
    assert "Thu 1 Oct" in card
    assert "Assumed" not in card
    assert 'data-date-source="stated"' in card
    assert confirmed.status_code == 200
    assert "to Thu 1 Oct" in confirmed.text
    (stored,) = drafts.all_of(user_id)
    assert (stored.status, stored.draft.occurred_on) == ("confirmed", _YESTERDAY)


async def test_editing_a_grade_from_6b_to_6c_keeps_scale_font() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    saved = client.post(
        f"/drafts/{draft.id}/edit", data=_form(**{"activities.1.grade": "6C"}), headers=_HTMX
    )

    assert saved.status_code == 200
    assert "6C" in _card(saved.text)
    (stored,) = drafts.all_of(user_id)
    payload = ClimbingPayload.model_validate(stored.draft.payload)
    assert payload.activities[1].grade == Grade(original="6C", scale="font")
    assert stored.status == "pending"


async def test_an_rpe_of_11_is_rejected_inline_and_the_draft_is_unchanged() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        data=_form(rpe="11", occurred_on="2026-10-01", **{"activities.1.grade": "6C"}),
        headers=_HTMX,
    )

    assert response.status_code == 422
    form = _edit_form(response.text)
    rpe = _input(form, "rpe")
    assert 'aria-invalid="true"' in rpe
    assert 'value="11"' in rpe
    assert "1 to 10" in form
    # What the athlete typed stays in the form, and only the bad field is marked.
    assert 'value="2026-10-01"' in _input(form, "occurred_on")
    assert 'value="6C"' in _input(form, "activities.1.grade")
    assert form.count('aria-invalid="true"') == 1
    assert [d.draft for d in drafts.all_of(user_id)] == [draft.draft]


async def test_a_bad_activity_field_is_marked_on_its_own_row() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    response = client.post(
        f"/drafts/{draft.id}/edit", data=_form(**{"activities.1.count": "0"}), headers=_HTMX
    )

    assert response.status_code == 422
    form = _edit_form(response.text)
    assert 'aria-invalid="true"' in _input(form, "activities.1.count")
    assert 'aria-invalid="true"' not in _input(form, "activities.0.count")
    assert [d.draft for d in drafts.all_of(user_id)] == [draft.draft]


async def test_the_form_swaps_its_validation_errors_in_and_ignores_a_refusal() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    form = _edit_form(client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text)

    (tag,) = re.findall(r"<form[^>]*>", form)
    assert 'hx-target="closest article"' in tag
    assert 'hx-swap="outerHTML"' in tag
    assert 'hx-status:404="swap:none"' in tag
    assert 'hx-status:409="swap:none"' in tag


async def test_cancel_restores_the_card_as_it_was() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    before = _card(client.get("/", headers=_HTMX).text)
    form = _edit_form(client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text)

    cancel = _button(form, "Cancel")
    assert f'hx-get="/drafts/{draft.id}"' in cancel
    assert 'type="button"' in cancel
    response = client.get(f"/drafts/{draft.id}", headers=_HTMX)

    assert response.status_code == 200
    assert _card(response.text) == before
    assert [d.draft for d in drafts.all_of(user_id)] == [draft.draft]


async def test_cancel_after_a_rejected_save_shows_the_card_as_stored() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    before = _card(client.get("/", headers=_HTMX).text)
    client.post(f"/drafts/{draft.id}/edit", data=_form(rpe="11"), headers=_HTMX)

    response = client.get(f"/drafts/{draft.id}", headers=_HTMX)

    assert _card(response.text) == before


async def test_a_resolved_draft_is_shown_as_its_row() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    response = client.get(f"/drafts/{draft.id}", headers=_HTMX)

    assert response.status_code == 200
    assert "data-confirmed-draft" in response.text
    assert 'aria-label="Draft session"' not in response.text


@pytest.mark.parametrize("resolve", ["confirm", "discard"])
async def test_editing_a_resolved_draft_is_refused_and_changes_nothing(resolve: str) -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    client.post(f"/drafts/{draft.id}/{resolve}", headers=_HTMX)
    (resolved,) = drafts.all_of(user_id)

    opened = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX)
    saved = client.post(
        f"/drafts/{draft.id}/edit", data=_form(occurred_on="2026-10-01"), headers=_HTMX
    )

    assert (opened.status_code, saved.status_code) == (409, 409)
    assert drafts.all_of(user_id) == [resolved]


async def test_another_users_draft_is_not_found_by_any_edit_route_and_is_unchanged() -> None:
    client, _, chat, drafts = _client()
    other = uuid4()
    draft = await _draft(chat, drafts, other)

    shown = client.get(f"/drafts/{draft.id}", headers=_HTMX)
    opened = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX)
    saved = client.post(f"/drafts/{draft.id}/edit", data=_form(rpe="5"), headers=_HTMX)

    assert (shown.status_code, opened.status_code, saved.status_code) == (404, 404, 404)
    assert drafts.all_of(other) == [draft]


@pytest.mark.parametrize("raw_id", ["not-a-uuid", str(uuid4())])
def test_a_malformed_or_unknown_draft_id_is_not_found(raw_id: str) -> None:
    client, _, _, _ = _client()

    assert client.get(f"/drafts/{raw_id}", headers=_HTMX).status_code == 404
    assert client.get(f"/drafts/{raw_id}/edit", headers=_HTMX).status_code == 404
    assert client.post(f"/drafts/{raw_id}/edit", data=_form(), headers=_HTMX).status_code == 404


async def test_a_draft_whose_payload_no_longer_reads_is_not_found_by_the_edit_routes() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    unreadable = draft.draft.model_copy(update={"payload": {"activities": []}})
    drafts.rows = [(owner, d.model_copy(update={"draft": unreadable})) for owner, d in drafts.rows]

    assert client.get(f"/drafts/{draft.id}", headers=_HTMX).status_code == 404
    assert client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).status_code == 404


async def test_an_edit_without_the_csrf_token_is_refused_and_changes_nothing() -> None:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), drafts=drafts)
    user = sign_in_as(app)
    app.add_middleware(CsrfMiddleware, secret_key=b"unit-test-secret", templates=build_templates())
    draft = await _draft(chat, drafts, user.id)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post(f"/drafts/{draft.id}/edit", data=_form(rpe="5"), headers=_HTMX)

    assert response.status_code == 403
    assert drafts.all_of(user.id) == [draft]


def test_form_changes_nests_the_activity_rows_by_position() -> None:
    assert form_changes(
        [
            ("rpe", "7"),
            ("activities.1.grade", "6B"),
            ("activities.0.count", "3"),
            ("activities.0.grade", "6A"),
        ]
    ) == {
        "rpe": "7",
        "activities": [{"count": "3", "grade": "6A"}, {"grade": "6B"}],
    }


@pytest.mark.parametrize(
    "pairs",
    [
        [("activities.x.count", "3")],
        [("activities.0", "3")],
        [("activities.99.count", "3")],
        [("activities.-1.count", "3")],
        [("a.b.c.d", "3")],
    ],
)
def test_form_changes_drops_keys_that_arent_a_field_or_a_row_field(
    pairs: list[tuple[str, str]],
) -> None:
    assert form_changes(pairs) == {}
