"""The signed-in glass app shell (`layouts/app.html`, ADR-0019, ticket #40): dock vs. sidebar
vs. the chat page's header menu, admin-only visibility, and 44px tap targets. Uses the same
`ActiveUserGateMiddleware` fakes as `test_active_user_gate.py`."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
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

WEB = Path(__file__).parents[2] / "src" / "ai_trainer" / "web"
TEMPLATES = WEB / "templates"
STATIC = WEB / "static"
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

    assert "data-dock" not in response.text
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

    assert "data-dock" in response.text
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


def _nav_block(html: str, opening_marker: str, closing_tag: str) -> str:
    start = html.index(opening_marker)
    return html[start : html.index(closing_tag, start)]


def _links(block: str) -> list[str]:
    return block.split("<a")[1:]


def _classes(link: str) -> set[str]:
    return set(link.split('class="')[1].split('"')[0].split())


def test_sidebar_rows_are_lg_nav_with_the_active_one_tinted() -> None:
    """The sidebar stays `mat-content` (#213); its section rows are `lg-nav` (Liquid Glass v4,
    #267): the active one a tinted lens with `aria-current`, the others ghost rows."""
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    sidebar = _nav_block(html, 'aria-label="Primary"', "</ul>")

    links = _links(sidebar)
    assert len(links) == 3
    for link in links:
        assert {"lg", "lg-nav"} <= _classes(link)
        assert 'class="size-5"' in link
    (active,) = [link for link in links if 'aria-current="page"' in link]
    assert 'href="/admin"' in active
    assert "lg-tinted" in _classes(active)
    for link in links:
        if link is not active:
            assert "lg-ghost" in _classes(link)


def test_sidebar_is_content_material() -> None:
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    start = html.rindex("<aside", 0, html.index('aria-label="Primary"'))
    aside = html[start : html.index(">", html.index('aria-label="Primary"'))]

    assert "mat-content" in aside
    assert "w-72" in aside
    assert "glass-thick" not in aside


def test_only_the_chat_pages_carry_the_conversation_list() -> None:
    client = _client(admin_emails=[USER_EMAIL])

    assert 'id="conversation-list-desktop"' in client.get("/").text
    admin = client.get("/admin").text
    assert "conversation-list-" not in admin
    assert 'class="drawer"' not in admin


def test_chat_menu_is_a_thick_surface_of_lg_nav_rows() -> None:
    html = _client(admin_emails=[USER_EMAIL]).get("/").text
    menu_tag = _nav_block(html, 'id="sections-menu"', ">")
    menu = _nav_block(html, 'id="sections-menu"', "</ul>")

    assert {"lgs", "lgs-thick"} <= _classes(menu_tag)
    links = _links(menu)
    assert len(links) == 3
    for link in links:
        assert {"lg", "lg-nav"} <= _classes(link)
        assert 'class="size-5"' in link
    (active,) = [link for link in links if 'aria-current="page"' in link]
    assert 'href="/"' in active
    assert "lg-tinted" in _classes(active)
    for link in links:
        if link is not active:
            assert "lg-ghost" in _classes(link)


def test_chat_menu_trigger_is_a_clear_icon_lens() -> None:
    html = _client(admin_emails=[]).get("/").text
    trigger = html[html.rindex("<button", 0, html.index("data-sections-trigger")) :]
    trigger = trigger[: trigger.index(">")]

    assert {"lg", "lg-clear", "lg-icon"} <= _classes(trigger)
    assert "btn-circle" not in trigger


def test_dock_is_an_lgs_capsule_with_the_active_item_a_tinted_tab() -> None:
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    dock_tag = _nav_block(html, "<nav\n      class=", ">")
    dock = _nav_block(html, "data-dock", "</nav>")

    assert {"lgs", "h-16", "rounded-full"} <= _classes(dock_tag)
    assert "lg:hidden" in _classes(dock_tag)
    links = _links(dock)
    assert len(links) == 3
    for link in links:
        assert 'class="size-5"' in link
    (active,) = [link for link in links if 'aria-current="page"' in link]
    assert 'href="/admin"' in active
    assert {"lg", "lg-tinted", "lg-tab"} <= _classes(active)
    for link in links:
        if link is not active:
            assert _classes(link) == {"lg-dock-item"}


def test_admin_is_an_extra_dock_item_for_admins() -> None:
    """The dock and the sidebar share `nav_sections`, which `visible_sections` filters by
    admin rights, so a member's chat page (no dock) shows no Admin link anywhere."""
    admin = _nav_block(_client(admin_emails=[USER_EMAIL]).get("/admin").text, "data-dock", "</nav>")

    assert len(_links(admin)) == 3
    assert 'href="/admin"' in admin


def test_dock_floats_above_the_safe_area() -> None:
    """The mock floats the dock 20px above the safe area, so the inset goes into the offset
    (#58); its items share the width with `flex-1`, so six fit at 390px (#267)."""
    html = _client(admin_emails=[USER_EMAIL]).get("/admin").text
    dock_tag = _nav_block(html, "<nav\n      class=", ">")

    assert "inset-x-3" in dock_tag
    assert "bottom-[calc(1.25rem+env(safe-area-inset-bottom))]" in dock_tag


def test_the_shell_has_no_v1_glass_or_daisyui_nav_classes() -> None:
    source = (TEMPLATES / "layouts" / "app.html").read_text()

    for leftover in ("glass-", "btn-circle", "menu-active", "dock-label", "min-h-11"):
        assert leftover not in source, leftover


def test_nav_utilities_keep_44px_targets() -> None:
    css = (STATIC / "css" / "glass.css").read_text()

    for name, height in (("lg-nav", "44px"), ("lg-tab", "52px"), ("lg-dock-item", "52px")):
        block = css.split(f"@utility {name} ", 1)[1].split("\n}\n", 1)[0]
        assert f"height: {height}" in block, name


def test_chat_section_stays_current_on_a_conversation_url() -> None:
    """`/c/new` and `/c/<id>` are the chat too (ADR-0021), so the Chat section keeps its mark
    there, on a full load as after an htmx swap from `/` (#213 skeptic finding)."""
    html = _client(admin_emails=[]).get("/c/new").text
    sidebar = _nav_block(html, 'aria-label="Primary"', "</ul>")

    (active,) = [link for link in sidebar.split("<a")[1:] if 'aria-current="page"' in link]
    assert 'href="/"' in active
    assert "lg-tinted" in _classes(active)
