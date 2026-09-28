"""Wires `GET /settings` (ADR-0019, ticket #41) and `POST /settings/delete-account`
(ticket #17, G7). The theme choice is applied and stored entirely client-side by
`static/js/theme.js`; `GET /settings` itself carries no theme state (nothing is stored
server-side)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.settings import build_settings_router
from ai_trainer.web.templating import STATIC_DIR, build_templates

FROZEN_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeUsersRepository:
    def __init__(self, *users: User) -> None:
        self._users = {user.id: user for user in users}
        self.delete_calls: list[UUID] = []

    async def get_by_sub(self, sub: str) -> User | None:
        raise NotImplementedError

    async def get(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)

    async def create(self, new_user: NewUser) -> User:
        raise NotImplementedError

    async def list_all(self) -> list[User]:
        return list(self._users.values())

    async def delete(self, user_id: UUID) -> None:
        self.delete_calls.append(user_id)
        self._users.pop(user_id, None)


class FakeSessionsRepository:
    def __init__(self, *sessions: Session) -> None:
        self._sessions = {session.id: session for session in sessions}

    async def create(self, new_session: NewSession) -> Session:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> Session | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: UUID) -> None:
        self._sessions.pop(session_id, None)

    async def delete_for_user(self, user_id: UUID) -> None:
        raise NotImplementedError


def _user() -> User:
    return User(
        id=uuid4(),
        sub="google-sub-1",
        email="athlete@example.com",
        name="Athlete",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
    )


def _session(user: User) -> Session:
    return Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_settings_router(build_templates(), users=FakeUsersRepository()))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return TestClient(app)


def _authenticated_client() -> tuple[TestClient, User, FakeUsersRepository]:
    """A client behind `ActiveUserGateMiddleware`, so `request.state.user` is set the same
    way it is in production, and a fake `UsersRepositoryPort` whose `delete` calls are
    observable."""
    user = _user()
    session = _session(user)
    users = FakeUsersRepository(user)
    sessions = FakeSessionsRepository(session)
    app = FastAPI()
    templates = build_templates()
    app.include_router(build_settings_router(templates, users=users))
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=[],
    )
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    return client, user, users


def test_get_settings_returns_full_page_with_appearance_section() -> None:
    client = _client()

    response = client.get("/settings")

    assert response.status_code == 200
    assert "<html" in response.text
    assert '<h2 class="text-[13px] font-medium text-base-content/70 px-1">Appearance</h2>' in (
        response.text
    )


def test_get_settings_with_hx_request_returns_partial_without_html_element() -> None:
    client = _client()

    response = client.get("/settings", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "<!doctype" not in response.text.lower()


def test_theme_toggle_renders_dark_and_light_buttons_with_aria_pressed() -> None:
    client = _client()

    response = client.get("/settings")

    assert 'data-theme-option="trainer-dark"' in response.text
    assert 'data-theme-option="trainer-light"' in response.text
    assert response.text.count('aria-pressed="true"') == 1
    assert response.text.count('aria-pressed="false"') == 1


def test_theme_toggle_defaults_to_dark_pressed() -> None:
    """The server always renders the `trainer-dark` default (ADR-0019): the actual stored
    choice is only known in the browser, and the pre-paint script in `base.html` plus
    `theme.js` correct the control's visual/aria state on load when it differs."""
    client = _client()

    response = client.get("/settings")

    dark_start = response.text.index('data-theme-option="trainer-dark"')
    dark_tag_start = response.text.rindex("<button", 0, dark_start)
    dark_tag_end = response.text.index(">", dark_start)
    dark_button = response.text[dark_tag_start:dark_tag_end]

    assert 'aria-pressed="true"' in dark_button


def test_theme_toggle_buttons_carry_no_htmx_attributes() -> None:
    """The choice makes no request to the server (AC): the toggle is pure client-side JS,
    never an htmx `hx-post`/`hx-get`."""
    client = _client()

    response = client.get("/settings")

    section_start = response.text.index('aria-label="Appearance"')
    section_end = response.text.index("</section>", section_start)
    section_html = response.text[section_start:section_end]

    assert "hx-post" not in section_html
    assert "hx-get" not in section_html


def test_get_settings_references_theme_script() -> None:
    client = _client()

    response = client.get("/settings")

    assert '<script src="/static/js/theme.js" defer></script>' in response.text


def test_get_settings_does_not_delete_anything() -> None:
    """AC: opening the page (and, by extension, cancelling the modal without ever posting)
    deletes nothing -- `GET /settings` never calls into `delete_account`."""
    client, _, users = _authenticated_client()

    response = client.get("/settings")

    assert response.status_code == 200
    assert users.delete_calls == []


def test_confirming_deletion_calls_delete_account_and_clears_the_cookie() -> None:
    client, user, users = _authenticated_client()

    response = client.post("/settings/delete-account", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/"
    assert users.delete_calls == [user.id]
    set_cookie = response.headers["set-cookie"]
    assert 'session_id=""' in set_cookie or "session_id=;" in set_cookie


def test_confirming_deletion_without_hx_request_redirects_to_home() -> None:
    client, _, _ = _authenticated_client()

    response = client.post("/settings/delete-account", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
