"""The draft routes record feedback events (ADR-0034, ticket #394): the edit, discard and undo
POSTs each write one content-free row, and a refused request writes none."""

from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FixedClock,
    InMemoryDraftRepository,
    ScriptedConversation,
    include_chat,
    pending_draft,
    sign_in_as,
)
from tests.feedback_support import InMemoryFeedbackEvents

_HTMX = {"HX-Request": "true"}
_TYPED = "felt great, new PB on the left hand"


def _client() -> tuple[TestClient, UUID, InMemoryDraftRepository, InMemoryFeedbackEvents]:
    app = FastAPI()
    drafts, events = InMemoryDraftRepository(), InMemoryFeedbackEvents()
    include_chat(
        app,
        build_templates(),
        conversation=ScriptedConversation(),
        drafts=drafts,
        feedback=FeedbackRecorder(events, clock=FixedClock()),
    )
    return TestClient(app), sign_in_as(app).id, drafts, events


def _edit_form(**changes: str) -> dict[str, str]:
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
    form.update(changes)
    return form


async def test_saving_an_edit_records_the_changed_field_names_and_no_value() -> None:
    client, user_id, drafts, events = _client()
    draft = await pending_draft(drafts, user_id)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        data=_edit_form(**{"activities.0.count": "4", "feeling": _TYPED}),
        headers=_HTMX,
    )

    assert response.status_code == 200
    [event] = events.events
    assert event.kind is FeedbackKind.DRAFT_EDITED
    assert set(event.fields) == {"count", "feeling"}
    assert _TYPED not in event.model_dump_json()


async def test_a_rejected_edit_records_nothing() -> None:
    client, user_id, drafts, events = _client()
    draft = await pending_draft(drafts, user_id)

    response = client.post(f"/drafts/{draft.id}/edit", data=_edit_form(rpe="eleven"), headers=_HTMX)

    assert response.status_code == 422
    assert events.events == []


async def test_discarding_records_one_draft_discarded() -> None:
    client, user_id, drafts, events = _client()
    draft = await pending_draft(drafts, user_id)

    client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)
    client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)

    assert [e.kind for e in events.events] == [FeedbackKind.DRAFT_DISCARDED]


async def test_undoing_records_one_session_undone() -> None:
    client, user_id, drafts, events = _client()
    draft = await pending_draft(drafts, user_id, status="confirmed")

    response = client.post(f"/drafts/{draft.id}/undo", headers=_HTMX)

    assert response.status_code == 200
    assert [e.kind for e in events.events] == [FeedbackKind.SESSION_UNDONE]


async def test_another_users_draft_records_nothing() -> None:
    client, _, drafts, events = _client()
    draft = await pending_draft(drafts, UUID(int=1))

    assert client.post(f"/drafts/{draft.id}/discard", headers=_HTMX).status_code == 404
    assert (
        client.post(f"/drafts/{draft.id}/edit", data=_edit_form(), headers=_HTMX).status_code == 404
    )

    assert events.events == []
