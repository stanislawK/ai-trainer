"""The admin's tokens page through the real middleware stack and PostgreSQL (ADR-0036, #455):
create once, reload shows the prefix, the 30-day cap, revoke, admin only and CSRF-checked."""

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.admin_token_repository import SqlAlchemyAdminTokensRepository
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.admin_tokens import AdminTokens
from ai_trainer.domain.admin_tokens import NewAdminToken
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.templating import build_templates
from tests.knowledge_support import knowledge_wiring

SECRET = b"integration-test-secret"

_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')
_TOKEN_IN_HTML = re.compile(r"atk_[A-Za-z0-9_-]{43}")


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at


@dataclass
class Page:
    client: TestClient
    factory: Callable[[], AsyncSession]
    admin: User
    csrf: str
    clock: Clock

    def headers(self) -> Mapping[str, str]:
        return {CSRF_HEADER_NAME: self.csrf, "HX-Request": "true"}

    def create(self, days: str = "30") -> str:
        response = self.client.post("/admin/tokens", data={"days": days}, headers=self.headers())
        assert response.status_code == 200, response.text
        [token] = {str(found) for found in _TOKEN_IN_HTML.findall(response.text)}
        return token


async def _page(factory: Callable[[], AsyncSession], sub: str, *, admin: bool = True) -> Page:
    users = SqlAlchemyUsersRepository(factory)
    sessions = SqlAlchemySessionsRepository(factory)
    clock = Clock()
    user = await users.create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )
    session = await sessions.create(
        # Long enough to outlive a token, so a test can move the clock past its expiry.
        NewSession(user_id=user.id, expires_at=clock.at + timedelta(days=7))
    )
    templates = build_templates()
    app = FastAPI()
    app.include_router(
        build_admin_router(
            templates=templates,
            users=users,
            sessions=sessions,
            status_changer=SqlAlchemyUserStatusChanger(factory),
            tokens=AdminTokens(tokens=SqlAlchemyAdminTokensRepository(factory), clock=clock),
            **knowledge_wiring(),
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=clock,
        templates=templates,
        admin_emails=[user.email] if admin else [],
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    match = _CSRF_TOKEN_IN_HTML.search(client.get("/admin/tokens").text)
    assert match is not None
    return Page(client=client, factory=factory, admin=user, csrf=match.group(1), clock=clock)


async def _rows(page: Page) -> list[Mapping[str, object]]:
    async with page.factory() as session:
        result = await session.execute(
            text("SELECT * FROM admin_tokens WHERE user_id = :id"), {"id": page.admin.id}
        )
        return [dict(row) for row in result.mappings()]


def _token_id(html: str) -> UUID:
    match = re.search(r"/admin/tokens/([0-9a-f-]{36})/revoke", html)
    assert match is not None, html
    return UUID(match.group(1))


async def test_create_shows_the_token_once_and_a_reload_shows_only_its_prefix(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-create")

    response = page.client.post("/admin/tokens", data={"days": "7"}, headers=page.headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    [token] = set(_TOKEN_IN_HTML.findall(response.text))
    for reload in (
        page.client.get("/admin/tokens"),
        page.client.get("/admin/tokens", headers={"HX-Request": "true"}),
    ):
        assert reload.status_code == 200
        assert token not in reload.text
        assert _TOKEN_IN_HTML.search(reload.text) is None
        assert token[:12] in reload.text


async def test_the_stored_row_holds_the_hash_never_the_token(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-hash")

    token = page.create("14")

    [row] = await _rows(page)
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert row["display_prefix"] == token[:12]
    assert row["expires_at"] == page.clock.at + timedelta(days=14)
    assert all(token not in str(value) for value in row.values())


@pytest.mark.parametrize(
    "days",
    ["31", "0", "abc", "", "30.5", "1" * 5000],
    ids=["31", "0", "abc", "blank", "frac", "5000-digits"],
)
async def test_an_expiry_past_30_days_is_refused(
    db_session_factory: Callable[[], AsyncSession], days: str
) -> None:
    # 5000 digits is past Python's int() conversion limit (4300), which must not become a 500.
    page = await _page(db_session_factory, f"tokens-expiry-{uuid4().hex[:8]}")

    response = page.client.post("/admin/tokens", data={"days": days}, headers=page.headers())

    assert response.status_code == 422
    assert _TOKEN_IN_HTML.search(response.text) is None
    assert "data-token-error" in response.text
    assert await _rows(page) == []


async def test_a_missing_expiry_is_refused(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-expiry-missing")

    response = page.client.post("/admin/tokens", data={}, headers=page.headers())

    assert response.status_code == 422
    assert await _rows(page) == []


async def test_revoke_marks_the_token_revoked_in_the_list(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-revoke")
    token = page.create()
    listed = page.client.get("/admin/tokens").text
    token_id = _token_id(listed)
    assert "Active" in listed

    response = page.client.post(f"/admin/tokens/{token_id}/revoke", headers=page.headers())

    assert response.status_code == 200
    assert "Revoked" in response.text
    assert f"/admin/tokens/{token_id}/revoke" not in response.text
    assert token[:12] in response.text
    [row] = await _rows(page)
    assert row["revoked_at"] == page.clock.at


async def test_revoking_another_admins_token_is_404(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _page(db_session_factory, "tokens-owner")
    intruder = await _page(db_session_factory, "tokens-intruder")
    owner.create()
    token_id = _token_id(owner.client.get("/admin/tokens").text)

    response = intruder.client.post(f"/admin/tokens/{token_id}/revoke", headers=intruder.headers())

    assert response.status_code == 404
    assert owner.create()[:12] not in intruder.client.get("/admin/tokens").text
    [first, _] = sorted(await _rows(owner), key=lambda row: str(row["created_at"]))
    assert first["revoked_at"] is None


async def test_revoking_an_unknown_or_malformed_id_is_404(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-unknown")

    for token_id in (str(uuid4()), "not-a-uuid"):
        response = page.client.post(f"/admin/tokens/{token_id}/revoke", headers=page.headers())
        assert response.status_code == 404


async def test_revoking_an_expired_token_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-expired")
    page.create("1")
    token_id = _token_id(page.client.get("/admin/tokens").text)
    page.clock.at += timedelta(days=2)

    response = page.client.post(f"/admin/tokens/{token_id}/revoke", headers=page.headers())

    assert response.status_code == 200
    assert "Expired" in response.text
    [row] = await _rows(page)
    assert row["revoked_at"] is None


async def test_the_list_shows_prefix_expiry_last_use_and_state(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-columns")
    used_at = page.clock.at - timedelta(hours=3)
    async with page.factory() as session:
        await SqlAlchemyAdminTokensRepository(page.factory).add(
            NewAdminToken(
                id=uuid4(),
                user_id=page.admin.id,
                token_hash=hashlib.sha256(b"used").hexdigest(),
                display_prefix="atk_UsedOne1",
                created_at=page.clock.at - timedelta(days=1),
                expires_at=page.clock.at + timedelta(days=6),
            )
        )
        await session.execute(
            text(
                "UPDATE admin_tokens SET last_used_at = :at WHERE display_prefix = 'atk_UsedOne1'"
            ),
            {"at": used_at},
        )
        await session.commit()

    html = page.client.get("/admin/tokens").text

    assert "atk_UsedOne1" in html
    assert "9 Nov" in html  # expires
    assert "3 Nov" in html  # last used
    assert "Active" in html


async def test_tokens_page_with_no_tokens_shows_the_empty_card(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "tokens-empty")

    html = page.client.get("/admin/tokens").text

    assert "data-tokens-empty" in html


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("GET", "/admin/tokens", None),
        ("POST", "/admin/tokens", {"days": "abc"}),
        ("POST", "/admin/tokens", {"days": "7"}),
        ("POST", "/admin/tokens/not-a-uuid/revoke", None),
        ("POST", f"/admin/tokens/{uuid4()}/revoke", None),
    ],
    ids=["list", "create-malformed", "create", "revoke-malformed", "revoke"],
)
async def test_a_non_admin_gets_403_on_every_tokens_route(
    db_session_factory: Callable[[], AsyncSession],
    method: str,
    path: str,
    data: Mapping[str, str] | None,
) -> None:
    page = await _page(db_session_factory, f"tokens-non-admin-{uuid4().hex[:8]}", admin=False)

    response = page.client.request(method, path, data=data, headers=page.headers())

    assert response.status_code == 403
    assert _TOKEN_IN_HTML.search(response.text) is None
    assert await _rows(page) == []


@pytest.mark.parametrize(
    "path", ["/admin/tokens", f"/admin/tokens/{uuid4()}/revoke"], ids=["create", "revoke"]
)
async def test_tokens_posts_without_csrf_are_403(
    db_session_factory: Callable[[], AsyncSession], path: str
) -> None:
    page = await _page(db_session_factory, f"tokens-csrf-{uuid4().hex[:8]}")

    response = page.client.post(path, data={"days": "7"}, headers={"HX-Request": "true"})

    assert response.status_code == 403
    assert await _rows(page) == []
