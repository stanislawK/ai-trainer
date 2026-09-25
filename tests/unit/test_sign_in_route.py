"""`GET /sign-in` renders the designed sign-in page on `layouts/bare.html` (ADR-0019,
ticket #43). The redirect-when-already-signed-in branch needs a real session, so it's covered
in `tests/integration/test_sign_in_redirect.py` instead."""

from datetime import datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User
from ai_trainer.web.sign_in import build_sign_in_router
from ai_trainer.web.templating import STATIC_DIR, build_templates


class _NoSessionUsers:
    """Every method raises: this file never sends a session cookie, so `build_sign_in_router`
    must not touch the repository at all before rendering the page."""

    async def get(self, user_id: UUID) -> User | None:
        raise AssertionError("no cookie means no repository call")

    async def get_by_sub(self, sub: str) -> User | None:
        raise AssertionError("no cookie means no repository call")

    async def create(self, new_user: NewUser) -> User:
        raise AssertionError("no cookie means no repository call")

    async def list_all(self) -> list[User]:
        raise AssertionError("no cookie means no repository call")


class _NoSessionSessions:
    async def get(self, session_id: UUID) -> Session | None:
        raise AssertionError("no cookie means no repository call")

    async def create(self, new_session: NewSession) -> Session:
        raise AssertionError("no cookie means no repository call")

    async def delete(self, session_id: UUID) -> None:
        raise AssertionError("no cookie means no repository call")

    async def delete_for_user(self, user_id: UUID) -> None:
        raise AssertionError("no cookie means no repository call")


class _FrozenClock:
    def now(self) -> datetime:
        raise AssertionError("no cookie means the clock is never consulted")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(
        build_sign_in_router(
            templates=build_templates(),
            users=_NoSessionUsers(),
            sessions=_NoSessionSessions(),
            clock=_FrozenClock(),
        )
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return TestClient(app)


def test_get_sign_in_returns_full_page_with_google_button() -> None:
    client = _client()

    response = client.get("/sign-in")

    assert response.status_code == 200
    assert "<html" in response.text
    assert 'href="/auth/login"' in response.text
    assert "Continue with Google" in response.text


def test_get_sign_in_with_hx_request_returns_partial_without_html_element() -> None:
    client = _client()

    response = client.get("/sign-in", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "<!doctype" not in response.text.lower()
    assert 'href="/auth/login"' in response.text


def test_sign_in_page_names_admin_approval() -> None:
    client = _client()

    response = client.get("/sign-in")

    assert "approved by an admin" in response.text


def test_sign_in_page_uses_the_bare_layout_with_no_app_shell() -> None:
    client = _client()

    response = client.get("/sign-in")

    assert 'aria-label="Primary"' not in response.text
    assert 'aria-label="Sections"' not in response.text


def test_sign_in_button_is_a_44px_tap_target() -> None:
    client = _client()

    response = client.get("/sign-in")

    button_start = response.text.index('href="/auth/login"')
    tag_start = response.text.rindex("<a", 0, button_start)
    tag_end = response.text.index(">", button_start)
    button_tag = response.text[tag_start:tag_end]

    assert "h-12" in button_tag


def test_sign_in_with_no_session_cookie_renders_the_page_without_touching_the_repositories() -> (
    None
):
    client = _client()

    response = client.get("/sign-in")

    assert response.status_code == 200


def test_sign_in_with_a_malformed_session_cookie_renders_the_page_without_a_repository_call() -> (
    None
):
    client = _client()
    client.cookies.set("session_id", "not-a-uuid")

    response = client.get("/sign-in")

    assert response.status_code == 200
