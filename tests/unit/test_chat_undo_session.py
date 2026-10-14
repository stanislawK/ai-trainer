"""Undoing a just-saved log from its confirmed row (PRD-0004 B4, ADR-0022, tickets #157 and
#165): `POST /drafts/{id}/undo` takes the draft's log back from its session, the row becomes
"Removed" and stays so on a reload, and someone else's or an unknown draft is left as it is."""

import re
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.sessions import NewSession, Session
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


def _client() -> tuple[TestClient, UUID, InMemoryChatRepository, InMemoryDraftRepository]:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), conversation=ScriptedConversation(), drafts=drafts)
    user = sign_in_as(app)
    return TestClient(app), user.id, chat, drafts


async def _draft(
    chat: InMemoryChatRepository, drafts: InMemoryDraftRepository, user_id: UUID
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
            occurred_on=THREE_6A_TWO_6B.occurred_on,
            date_source=THREE_6A_TWO_6B.date_source,
            timezone="Europe/Warsaw",
            payload=THREE_6A_TWO_6B.to_payload().model_dump(mode="json"),
        ),
        payload_version=1,
        created_at=FROZEN_NOW,
    )


async def _saved(
    client: TestClient,
    chat: InMemoryChatRepository,
    drafts: InMemoryDraftRepository,
    user_id: UUID,
) -> tuple[UUID, UUID]:
    """A draft the athlete confirmed, and the ID of the session that saved."""
    draft = await _draft(chat, drafts, user_id)
    client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)
    (stored,) = drafts.all_of(user_id)
    assert stored.session_id is not None
    return stored.id, stored.session_id


def _removed_rows(html: str) -> list[str]:
    return re.findall(r'<div[^>]*data-removed-draft="[^"]+".*?</div>', html, re.S)


async def test_undo_collapses_the_row_to_removed_and_removes_the_session() -> None:
    client, user_id, chat, drafts = _client()
    draft_id, session_id = await _saved(client, chat, drafts, user_id)

    response = client.post(f"/drafts/{draft_id}/undo", headers=_HTMX)

    assert response.status_code == 200
    (row,) = _removed_rows(response.text)
    assert "Removed" in row
    assert "<button" not in row
    assert "data-confirmed-draft" not in response.text
    (stored,) = drafts.all_of(user_id)
    assert (stored.status, stored.session_id) == ("confirmed", None)
    assert drafts.removed_sessions == [session_id]


async def test_the_undo_button_asks_once_before_it_posts_and_leaves_the_row_on_a_refusal() -> None:
    client, user_id, chat, drafts = _client()
    draft_id, _ = await _saved(client, chat, drafts, user_id)

    page = client.get("/", headers=_HTMX).text

    (button,) = re.findall(r"<button[^>]*>(?:(?!</button>).)*Undo</button>", page, re.S)
    assert f'hx-post="/drafts/{draft_id}/undo"' in button
    assert re.search(r'hx-confirm="[^"]+"', button)
    assert 'hx-disable="this"' in button
    assert 'hx-status:4xx="swap:none"' in button
    assert "disabled>" not in button.replace('hx-disable="this"', "")
    assert drafts.removed_sessions == []


async def test_a_reload_still_reads_removed() -> None:
    client, user_id, chat, drafts = _client()
    draft_id, _ = await _saved(client, chat, drafts, user_id)
    client.post(f"/drafts/{draft_id}/undo", headers=_HTMX)

    page = client.get("/").text

    assert len(_removed_rows(page)) == 1
    assert "data-confirmed-draft" not in page
    assert "Undo" not in page
    assert 'aria-label="Draft session"' not in page


async def test_another_users_draft_is_not_found_and_its_session_stays() -> None:
    client, _, chat, drafts = _client()
    owner = uuid4()
    draft = await _draft(chat, drafts, owner)
    drafts.set_status(draft.id, "confirmed")
    (stored,) = drafts.all_of(owner)

    response = client.post(f"/drafts/{stored.id}/undo", headers=_HTMX)

    assert response.status_code == 404
    assert [d.session_id for d in drafts.all_of(owner)] == [stored.session_id]
    assert drafts.removed_sessions == []


@pytest.mark.parametrize("raw_id", ["not-a-uuid", str(uuid4())])
def test_a_malformed_or_unknown_draft_id_is_not_found(raw_id: str) -> None:
    client, _, _, _ = _client()

    response = client.post(f"/drafts/{raw_id}/undo", headers=_HTMX)

    assert response.status_code == 404


async def test_undoing_twice_is_not_found_the_second_time() -> None:
    client, user_id, chat, drafts = _client()
    draft_id, _ = await _saved(client, chat, drafts, user_id)
    client.post(f"/drafts/{draft_id}/undo", headers=_HTMX)

    second = client.post(f"/drafts/{draft_id}/undo", headers=_HTMX)

    assert second.status_code == 404


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


async def test_a_signed_out_undo_is_refused_and_removes_nothing() -> None:
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
    drafts.set_status(draft.id, "confirmed")
    (stored,) = drafts.all_of(owner)

    response = TestClient(app, follow_redirects=False).post(
        f"/drafts/{stored.id}/undo", headers=_HTMX
    )

    assert response.status_code == 401
    assert drafts.removed_sessions == []


async def test_an_undo_without_the_csrf_token_is_refused_and_removes_nothing() -> None:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), drafts=drafts)
    user = sign_in_as(app)
    app.add_middleware(CsrfMiddleware, secret_key=b"unit-test-secret", templates=build_templates())
    draft = await _draft(chat, drafts, user.id)
    drafts.set_status(draft.id, "confirmed")
    (stored,) = drafts.all_of(user.id)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post(f"/drafts/{stored.id}/undo", headers=_HTMX)

    assert response.status_code == 403
    assert drafts.removed_sessions == []
