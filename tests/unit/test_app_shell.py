"""The signed-in glass app shell (`layouts/app.html`, ADR-0019, ticket #40): dock vs. sidebar
vs. the chat page's header menu, admin-only visibility, and 44px tap targets. Uses the same
`ActiveUserGateMiddleware` fakes as `test_active_user_gate.py`."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.home import build_home_router
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
USER_EMAIL = "athlete@example.com"


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeUsersRepository:
    def __init__(self, *users: User) -> None:
        self._users = {user.id: user for user in users}

    async def get_by_sub(self, sub: str) -> User | None:
        raise NotImplementedError

    async def get(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)

    async def create(self, new_user: NewUser) -> User:
        raise NotImplementedError


class FakeSessionsRepository:
    def __init__(self, *sessions: Session) -> None:
        self._sessions = {session.id: session for session in sessions}

    async def create(self, new_session: NewSession) -> Session:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> Session | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: UUID) -> None:
        raise NotImplementedError


def _client(*, admin_emails: list[str]) -> TestClient:
    user = User(
        id=uuid4(),
        sub="google-sub-1",
        email=USER_EMAIL,
        name="Athlete",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
    )
    session = Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )
    app = FastAPI()
    templates = build_templates()
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=FakeUsersRepository(user),
        sessions=FakeSessionsRepository(session),
        clock=FakeClock(),
        templates=templates,
        admin_emails=admin_emails,
    )
    app.include_router(build_home_router(templates))
    app.include_router(build_admin_router(templates))
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    return client


def test_chat_page_has_no_dock_and_shows_the_sections_menu_button() -> None:
    response = _client(admin_emails=[]).get("/")

    assert 'class="dock lg:hidden' not in response.text
    assert 'popovertarget="sections-menu"' in response.text
    assert 'id="sections-menu"' in response.text


def test_sections_menu_is_hidden_by_default_regardless_of_daisyui_menu_display() -> None:
    """`.menu` (daisyUI) sets `display: flex` unconditionally, which otherwise beats the
    browser's own `[popover]:not(:popover-open)` default — an author rule always outranks a
    user-agent rule regardless of specificity, so the popover rendered open on every page
    load until this was forced closed with `hidden!` (skeptic finding, ticket #40 review
    gate: caught only by `tests/e2e/test_chat_sections_menu.py`, not by any unit assertion —
    this pins the CSS-class mechanism the e2e test's runtime behavior depends on)."""
    response = _client(admin_emails=[]).get("/")

    menu_start = response.text.index('id="sections-menu"')
    menu_tag = response.text[menu_start : menu_start + 400]

    assert "hidden!" in menu_tag
    assert "[&:popover-open]:flex!" in menu_tag


def test_admin_page_shows_the_dock_and_no_menu_button() -> None:
    response = _client(admin_emails=[USER_EMAIL]).get("/admin")

    assert 'class="dock lg:hidden' in response.text
    assert 'popovertarget="sections-menu"' not in response.text


def test_sidebar_is_hidden_below_desktop_and_shown_from_it() -> None:
    """The sidebar must render on every signed-in page (chat included, per the mock: pattern
    2c hides only the mobile *dock* on chat, never the desktop sidebar) and stay collapsed
    below the 1024px `lg:` breakpoint (skeptic finding, ticket #40 review gate: nothing
    previously asserted these classes, so a regression dropping either one would pass)."""
    for href in ("/", "/admin"):
        response = _client(admin_emails=[USER_EMAIL]).get(href)

        assert 'aria-label="Primary"' in response.text
        assert 'class="hidden lg:flex' in response.text


def test_current_page_carries_aria_current() -> None:
    response = _client(admin_emails=[USER_EMAIL]).get("/admin")

    # The sidebar and the dock each render one nav_link per section; the Admin link (current)
    # gets `aria-current="page"` in both, the Chat link gets it in neither.
    assert response.text.count('aria-current="page"') == 2


def test_non_admin_never_sees_the_admin_link_anywhere_on_the_page() -> None:
    response = _client(admin_emails=[]).get("/")

    assert "Admin</a>" not in response.text
    assert 'href="/admin"' not in response.text


def test_admin_sees_the_admin_link() -> None:
    response = _client(admin_emails=[USER_EMAIL]).get("/")

    assert 'href="/admin"' in response.text


def test_sidebar_shows_the_signed_in_users_email_and_a_sign_out_button() -> None:
    response = _client(admin_emails=[]).get("/")

    assert USER_EMAIL in response.text
    assert 'hx-post="/auth/logout"' in response.text
    assert 'aria-label="Sign out"' in response.text


def test_sign_out_button_meets_the_44px_tap_target() -> None:
    """`btn-circle btn-sm` renders daisyUI's `--size-field`-based circle at 8 * 0.275rem =
    35.2px, under the 44px floor `min-h-11`/the default `.btn` size give every other nav
    target (skeptic finding, ticket #40 review gate: `test_dock_and_menu_items_meet_the_44px_
    tap_target`'s `min-h-11` count didn't cover this button, so the gap went undetected)."""
    response = _client(admin_emails=[]).get("/")

    button_start = response.text.index('aria-label="Sign out"')
    button_tag = response.text[max(0, button_start - 200) : button_start]

    assert "btn-sm" not in button_tag


def test_every_nav_target_on_the_chat_page_resolves() -> None:
    client = _client(admin_emails=[USER_EMAIL])

    for href in ("/", "/admin"):
        response = client.get(href)
        assert response.status_code == 200, href


def test_dock_and_menu_items_meet_the_44px_tap_target() -> None:
    response = _client(admin_emails=[USER_EMAIL]).get("/admin")

    # Every generated nav_link and the dock anchor itself carries min-h-11 (44px).
    assert response.text.count("min-h-11") >= 4
