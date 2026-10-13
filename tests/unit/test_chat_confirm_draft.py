"""Confirming a draft from its card (PRD-0004 F2, B2, ADR-0022, ticket #155): `POST
/drafts/{id}/confirm` saves it once, the card collapses to the Chat mock's "Logged … to <date>"
row (state 9), the row comes back on a reload, and someone else's or a discarded draft writes
nothing."""

import re
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction, ExtractedClimb
from ai_trainer.domain.sports.climbing.payload import RouteRef
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.domain.users import NewUser, User
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.drafts import draft_card
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    THREE_6A_TWO_6B,
    UNLOGGABLE_SPORT,
    FixedClock,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    ScriptedConversation,
    include_chat,
    pending_draft,
    sign_in_as,
    sports_with_rowing,
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


def _confirmed_rows(html: str) -> list[str]:
    return re.findall(r'<div[^>]*data-confirmed-draft="[^"]+".*?</div>', html, re.S)


async def test_confirm_saves_the_draft_and_collapses_the_card_to_its_logged_row() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert response.status_code == 200
    (row,) = _confirmed_rows(response.text)
    assert "Logged 5 climbs to Fri 2 Oct" in row
    assert 'aria-label="Draft session"' not in response.text
    (stored,) = drafts.all_of(user_id)
    assert (stored.status, drafts.confirmed_at) == ("confirmed", {draft.id: FROZEN_NOW})


@pytest.mark.parametrize(
    ("climb", "summary"),
    [
        (
            ExtractedClimb(
                kind="lead", count=1, grade="8a", style="redpoint", route=RouteRef(name="Hades")
            ),
            "Hades 8a",
        ),
        (ExtractedClimb(kind="lead", count=1, route=RouteRef(name="Hades")), "Hades"),
        (ExtractedClimb(kind="boulder", count=3, grade="6A"), f"3 {_TIMES} 6A"),
        (ExtractedClimb(kind="top_rope", count=4), f"4 {_TIMES} Top rope"),
    ],
)
async def test_one_activity_is_named_as_on_the_mock(climb: ExtractedClimb, summary: str) -> None:
    client, user_id, chat, drafts = _client()
    extraction = ClimbingExtraction(
        occurred_on=THREE_6A_TWO_6B.occurred_on, date_source="stated", activities=[climb]
    )
    draft = await _draft(chat, drafts, user_id, extraction)

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert f"Logged {summary} to Fri 2 Oct" in response.text


async def test_confirming_twice_shows_the_same_row_and_saves_once() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    first = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)
    second = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert second.status_code == 200
    assert _confirmed_rows(second.text) == _confirmed_rows(first.text)
    assert len(drafts.confirmed_at) == 1


async def test_the_confirm_button_posts_once_per_tap_and_leaves_the_card_on_a_refusal() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    page = client.get("/", headers=_HTMX).text

    (button,) = re.findall(r"<button[^>]*>(?:(?!</button>).)*Confirm</button>", page, re.S)
    assert f'hx-post="/drafts/{draft.id}/confirm"' in button
    assert 'hx-disable="this"' in button
    assert 'hx-status:4xx="swap:none"' in button
    assert "disabled>" not in button.replace('hx-disable="this"', "")


async def test_a_reload_shows_the_confirmed_row_not_the_pending_card() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    page = client.get("/").text

    assert len(_confirmed_rows(page)) == 1
    assert "Logged 5 climbs to Fri 2 Oct" in page
    assert 'aria-label="Draft session"' not in page


async def test_the_confirmed_rows_undo_posts_to_its_session() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)

    row = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX).text

    (stored,) = drafts.all_of(user_id)
    assert re.search(
        rf'<button[^>]*hx-post="/sessions/{stored.session_id}/undo"[^>]*>Undo</button>', row
    )
    assert not re.search(r"<button[^>]*\bdisabled>Undo</button>", row)


async def test_another_users_draft_is_not_found_and_stays_pending() -> None:
    client, _, chat, drafts = _client()
    other = uuid4()
    draft = await _draft(chat, drafts, other)

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert response.status_code == 404
    assert [d.status for d in drafts.all_of(other)] == ["pending"]
    assert drafts.confirmed_at == {}


@pytest.mark.parametrize("raw_id", ["not-a-uuid", str(uuid4())])
def test_a_malformed_or_unknown_draft_id_is_not_found(raw_id: str) -> None:
    client, _, _, _ = _client()

    response = client.post(f"/drafts/{raw_id}/confirm", headers=_HTMX)

    assert response.status_code == 404


async def test_confirming_a_discarded_draft_is_refused_and_writes_nothing() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    drafts.set_status(draft.id, "discarded")

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert response.status_code == 409
    assert [d.status for d in drafts.all_of(user_id)] == ["discarded"]
    assert drafts.confirmed_at == {}


async def test_the_athletes_text_in_the_logged_row_is_escaped() -> None:
    client, user_id, chat, drafts = _client()
    route = "<b>Hades</b>"
    extraction = ClimbingExtraction(
        occurred_on=THREE_6A_TWO_6B.occurred_on,
        date_source="stated",
        activities=[ExtractedClimb(kind="lead", count=1, route=RouteRef(name=route))],
    )
    draft = await _draft(chat, drafts, user_id, extraction)

    row = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX).text

    assert str(escape(route)) in row
    assert route not in row


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


async def test_a_signed_out_confirm_is_refused_and_writes_nothing() -> None:
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
        f"/drafts/{draft.id}/confirm", headers=_HTMX
    )

    assert response.status_code == 401
    assert drafts.confirmed_at == {}


async def test_a_confirm_without_the_csrf_token_is_refused_and_writes_nothing() -> None:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(app, build_templates(), drafts=drafts)
    user = sign_in_as(app)
    app.add_middleware(CsrfMiddleware, secret_key=b"unit-test-secret", templates=build_templates())
    draft = await _draft(chat, drafts, user.id)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert response.status_code == 403
    assert drafts.confirmed_at == {}


@pytest.mark.parametrize("sport_id", ["surfing", UNLOGGABLE_SPORT])
async def test_a_draft_of_an_unknown_sport_or_one_without_extraction_has_no_card(
    sport_id: str,
) -> None:
    drafts = InMemoryDraftRepository()
    draft = await pending_draft(drafts, uuid4())

    card = draft_card(
        draft.model_copy(update={"sport_id": sport_id}),
        sports=sports_with_rowing(),
        today=THREE_6A_TWO_6B.occurred_on,
    )

    assert card is None


async def test_a_confirmed_draft_whose_payload_no_longer_reads_still_shows_a_logged_row() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id)
    unreadable = draft.draft.model_copy(update={"payload": {"activities": []}})
    drafts.rows = [(owner, d.model_copy(update={"draft": unreadable})) for owner, d in drafts.rows]

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)
    reload = client.get("/").text

    assert response.status_code == 200
    for html in (response.text, reload):
        (row,) = _confirmed_rows(html)
        assert "Logged session to Fri 2 Oct" in row
