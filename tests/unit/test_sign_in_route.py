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


def _google_button_tag(html: str) -> str:
    button_start = html.index('href="/auth/login"')
    tag_start = html.rindex("<a", 0, button_start)
    tag_end = html.index(">", button_start)
    return html[tag_start:tag_end]


def test_sign_in_button_is_a_44px_tap_target_at_both_sizes() -> None:
    client = _client()

    response = client.get("/sign-in")

    button_classes = _google_button_tag(response.text).split()
    assert "h-14" in button_classes
    assert "lg:h-12" in button_classes


def test_sign_in_page_shows_the_redesigned_headline() -> None:
    client = _client()

    response = client.get("/sign-in")

    assert "Train with a coach that remembers every session." in response.text


def test_desktop_chat_preview_is_decorative_and_desktop_only() -> None:
    client = _client()

    response = client.get("/sign-in")

    preview_start = response.text.index('id="sign-in-preview-desktop"')
    tag_start = response.text.rindex("<div", 0, preview_start)
    preview_tag = response.text[tag_start : response.text.index(">", preview_start)]
    assert 'aria-hidden="true"' in preview_tag
    assert "hidden" in preview_tag.split('class="')[1].split()
    assert "lg:flex" in preview_tag


def test_google_button_carries_the_multicolor_g_mark_and_brand_fills() -> None:
    client = _client()

    response = client.get("/sign-in")

    button_start = response.text.index('href="/auth/login"')
    button_end = response.text.index("</a>", button_start)
    button_html = response.text[button_start:button_end]
    assert 'src="/static/brand/google-g.svg"' in button_html
    assert "Continue with Google" in button_html
    button_classes = _google_button_tag(response.text).split()
    # Google branding: near-black fill on the light theme, white fill on the dark theme.
    assert "bg-[#131314]" in button_classes
    assert "dark:bg-white" in button_classes


def test_google_g_mark_is_served_locally_in_all_four_brand_colors() -> None:
    client = _client()

    response = client.get("/static/brand/google-g.svg")

    assert response.status_code == 200
    for color in ("#EA4335", "#4285F4", "#FBBC05", "#34A853"):
        assert color in response.text


def test_google_button_ships_a_hidden_loading_state_and_its_script() -> None:
    client = _client()

    response = client.get("/sign-in")

    button_start = response.text.index('href="/auth/login"')
    button_html = response.text[button_start : response.text.index("</a>", button_start)]
    assert "Opening Google…" in button_html
    assert "loading-spinner" in button_html
    assert 'src="/static/js/sign_in.js"' in response.text
    assert client.get("/static/js/sign_in.js").status_code == 200


def test_sign_in_page_has_no_alert_without_an_error() -> None:
    client = _client()

    response = client.get("/sign-in")

    assert 'role="alert"' not in response.text


def test_failed_google_sign_in_shows_the_inline_alert_above_the_button_and_keeps_the_page() -> None:
    client = _client()

    response = client.get("/sign-in?error=google")

    assert response.status_code == 200
    alert_at = response.text.index('role="alert"')
    assert alert_at < response.text.index('href="/auth/login"')
    assert "Google sign-in didn\u2019t finish. Try again." in response.text
    assert "Train with a coach that remembers every session." in response.text
    assert "approved by an admin" in response.text


def test_failed_google_sign_in_alert_also_renders_in_the_htmx_partial() -> None:
    client = _client()

    response = client.get("/sign-in?error=google", headers={"HX-Request": "true"})

    assert 'role="alert"' in response.text


def test_an_unknown_error_value_shows_no_alert_and_is_never_echoed() -> None:
    client = _client()

    response = client.get("/sign-in?error=%3Cscript%3Eboom%3C%2Fscript%3E")

    assert 'role="alert"' not in response.text
    assert "boom" not in response.text


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
