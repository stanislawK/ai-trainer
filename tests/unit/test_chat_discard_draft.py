"""Discarding a draft from its card (PRD-0004 F2, ADR-0022, ticket #156): `POST
/drafts/{id}/discard` throws it away without saving a session, the card collapses to a
"Draft discarded" row that comes back on a reload, and someone else's or a confirmed draft is
left as it is."""

import re
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.domain.users import NewUser, User
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    THREE_6A_TWO_6B,
    FixedClock,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_CLIMB = "I climbed 3 6A and 2 6B boulders"
_TIMES = "\N{MULTIPLICATION SIGN}"


def _client() -> tuple[TestClient, UUID, InMemoryChatRepository, InMemoryDraftRepository]:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), conversation=ScriptedConversation(), drafts=drafts)
    user = sign_in_as(app)
    return TestClient(app), user.id, chat, drafts


async def _draft(
    chat: InMemoryChatRepository,
    drafts: InMemoryDraftRepository,
    user_id: UUID,
    extraction: ClimbingExtraction = THREE_6A_TWO_6B,
) -> StoredDraft:
    """The athlete's climbing log, its extraction reply, and the pending draft under it."""
    conversation_id = await chat.conversation_of(user_id)
    log = await chat.add(user_id, conversation_id, ChatRole.USER, _CLIMB, FROZEN_NOW)
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
            occurred_on=extraction.occurred_on,
            date_source=extraction.date_source,
            timezone="Europe/Warsaw",
            payload=extraction.to_payload().model_dump(mode="json"),
        ),
        payload_version=1,
        created_at=FROZEN_NOW,
    )


def _discarded_rows(html: str) -> list[str]:
    return re.findall(r'<div[^>]*data-discarded-draft="[^"]+".*?</div>', html, re.S)


async def test_discard_collapses_the_card_to_the_discarded_row_and_saves_no_session() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    response = client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)

    assert response.status_code == 200
    (row,) = _discarded_rows(response.text)
    assert "Draft discarded" in row
    assert "<button" not in row
    assert 'aria-label="Draft session"' not in response.text
    (stored,) = drafts.all_of(user_id)
    assert (stored.status, stored.session_id) == ("discarded", None)
    assert drafts.discarded_at == {draft.id: FROZEN_NOW}
    assert drafts.confirmed_at == {}


async def test_discarding_twice_shows_the_same_row() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    first = client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)
    second = client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)

    assert second.status_code == 200
    assert _discarded_rows(second.text) == _discarded_rows(first.text)


async def test_the_discard_button_posts_once_per_tap_and_leaves_the_card_on_a_refusal() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    page = client.get("/", headers=_HTMX).text

    (button,) = re.findall(r"<button[^>]*>(?:(?!</button>).)*Discard</button>", page, re.S)
    assert f'hx-post="/drafts/{draft.id}/discard"' in button
    assert 'hx-disable="this"' in button
    assert 'hx-status:4xx="swap:none"' in button
    assert "disabled>" not in button.replace('hx-disable="this"', "")


async def test_a_reload_shows_the_discarded_row_not_the_pending_card() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)

    page = client.get("/").text

    assert len(_discarded_rows(page)) == 1
    assert 'aria-label="Draft session"' not in page


async def test_a_discarded_draft_whose_payload_no_longer_reads_still_shows_the_row() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    unreadable = draft.draft.model_copy(update={"payload": {"activities": []}})
    drafts.rows = [(owner, d.model_copy(update={"draft": unreadable})) for owner, d in drafts.rows]

    response = client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)
    reload = client.get("/").text

    assert response.status_code == 200
    for html in (response.text, reload):
        assert len(_discarded_rows(html)) == 1


async def test_another_users_draft_is_not_found_and_stays_pending() -> None:
    client, _, chat, drafts = _client()
    other = uuid4()
    draft = await _draft(chat, drafts, other)

    response = client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)

    assert response.status_code == 404
    assert [d.status for d in drafts.all_of(other)] == ["pending"]
    assert drafts.discarded_at == {}


@pytest.mark.parametrize("raw_id", ["not-a-uuid", str(uuid4())])
def test_a_malformed_or_unknown_draft_id_is_not_found(raw_id: str) -> None:
    client, _, _, _ = _client()

    response = client.post(f"/drafts/{raw_id}/discard", headers=_HTMX)

    assert response.status_code == 404


async def test_discarding_a_confirmed_draft_is_refused_and_changes_nothing() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    response = client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)

    assert response.status_code == 409
    (stored,) = drafts.all_of(user_id)
    assert (stored.status, stored.session_id is not None) == ("confirmed", True)
    assert drafts.discarded_at == {}


# The route sits behind the app's middleware like every other: the gate and CSRF.


class _NoUsers:
    async def get_by_sub(self, sub: str) -> User | None:
        raise NotImplementedError

    async def get(self, user_id: UUID) -> User | None:
        return None

    async def create(self, new_user: NewUser) -> User:
        raise NotImplementedError

    async def list_all(self) -> list[User]:
        return []

    async def delete(self, user_id: UUID) -> None:
        raise NotImplementedError


class _NoSessions:
    async def create(self, new_session: NewSession) -> Session:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> Session | None:
        return None

    async def delete(self, session_id: UUID) -> None:
        return None

    async def delete_for_user(self, user_id: UUID) -> None:
        return None


async def test_a_signed_out_discard_is_refused_and_writes_nothing() -> None:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), drafts=drafts)
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=_NoUsers(),
        sessions=_NoSessions(),
        clock=FixedClock(),
        templates=build_templates(),
        admin_emails=[],
    )
    owner = uuid4()
    draft = await _draft(chat, drafts, owner)

    response = TestClient(app, follow_redirects=False).post(
        f"/drafts/{draft.id}/discard", headers=_HTMX
    )

    assert response.status_code == 401
    assert drafts.discarded_at == {}


async def test_a_discard_without_the_csrf_token_is_refused_and_writes_nothing() -> None:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), drafts=drafts)
    user = sign_in_as(app)
    app.add_middleware(CsrfMiddleware, secret_key=b"unit-test-secret", templates=build_templates())
    draft = await _draft(chat, drafts, user.id)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post(f"/drafts/{draft.id}/discard", headers=_HTMX)

    assert response.status_code == 403
    assert drafts.discarded_at == {}
