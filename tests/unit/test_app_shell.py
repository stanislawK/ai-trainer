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
from ai_trainer.web.templating import build_templates
from tests.chat_support import include_chat

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

    async def list_all(self) -> list[User]:
        return list(self._users.values())

    async def delete(self, user_id: UUID) -> None:
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

    async def delete_for_user(self, user_id: UUID) -> None:
        raise NotImplementedError


class FakeUserStatusChanger:
    async def change(
        self, *, target_user_id: UUID, new_status: UserStatus, actor_user_id: UUID
    ) -> tuple[User, UserStatus]:
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
        onboarded_at=FROZEN_NOW,
    )
    session = Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )
    app = FastAPI()
    templates = build_templates()
    users = FakeUsersRepository(user)
    sessions = FakeSessionsRepository(session)
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=admin_emails,
    )
    include_chat(app, templates)
    app.include_router(
        build_admin_router(
            templates=templates,
            users=users,
            sessions=sessions,
            status_changer=FakeUserStatusChanger(),
        )
    )
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


def _nav_block(html: str, opening_marker: str, closing_tag: str) -> str:
    start = html.index(opening_marker)
    return html[start : html.index(closing_tag, start)]


def test_sidebar_menu_items_center_icon_and_label_vertically() -> None:
    """daisyUI's menu item sets `align-content: flex-start`, pinning icon and label to the top
    of the 44px tap target; the Components Navigation mock adds `content-center` (#58)."""
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    sidebar = _nav_block(html, 'aria-label="Primary"', "</ul>")

    links = sidebar.split("<a")[1:]
    assert links
    for link in links:
        assert "content-center" in link.split(">")[0]
        assert 'class="size-5"' in link


def test_chat_menu_items_center_icon_and_label_vertically() -> None:
    html = _client(admin_emails=[USER_EMAIL]).get("/").text
    menu = _nav_block(html, 'id="sections-menu"', "</ul>")

    links = menu.split("<a")[1:]
    assert links
    for link in links:
        assert "content-center" in link.split(">")[0]
        assert 'class="size-5"' in link


def test_dock_icons_are_sized_so_the_label_clears_the_active_underline() -> None:
    """An unsized SVG grew to 36.5px in a dock item and pushed the label onto daisyUI's
    active underline; the mock's icons are 1.25rem (#58)."""
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    dock = _nav_block(html, 'class="dock lg:hidden', "</nav>")

    links = dock.split("<a")[1:]
    assert links
    for link in links:
        assert 'class="size-5"' in link


def test_dock_width_yields_to_its_side_insets() -> None:
    """daisyUI's `.dock` sets `width: 100%`, so with `inset-x-4` alone it spanned 16px→406px
    on a 390px screen, its right end off-canvas; the mock adds `w-auto` (#58)."""
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    dock_tag = _nav_block(html, 'class="dock lg:hidden', ">")

    assert "inset-x-4" in dock_tag
    assert "w-auto" in dock_tag


def test_dock_floats_above_the_safe_area_instead_of_padding_into_it() -> None:
    """daisyUI's `.dock` adds `padding-bottom: env(safe-area-inset-bottom)`; inside the fixed
    `h-16` that ate 34px on a home-indicator iPhone and put the label back on the underline.
    The mock floats the dock 20px above the safe area, so the inset goes into the offset
    (skeptic finding, #58 review gate)."""
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    dock_tag = _nav_block(html, 'class="dock lg:hidden', ">")

    assert "pb-0" in dock_tag
    assert "bottom-[calc(1.25rem+env(safe-area-inset-bottom))]" in dock_tag
    assert " bottom-5 " not in dock_tag
