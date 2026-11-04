"""The knowledge API behind admin tokens (ADR-0036, #457), through the app's own middleware
(rate limit, CSRF, the session gate) and real PostgreSQL."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.admin_token_repository import SqlAlchemyAdminTokensRepository
from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.pmc import PmcCloudMetadata
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.admin_tokens import AdminTokens, TokenAuthenticator
from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.application.knowledge_staging import KnowledgeStaging
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeFileType,
    LicenseId,
    LicenseSource,
    NewKnowledgeSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
    content_hash,
)
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.api import build_api_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.rate_limit import RateLimitMiddleware
from ai_trainer.web.templating import build_templates
from tests.pmc_support import BUCKET, FakeBucket

SECRET = b"integration-test-secret"
SOURCES = "/api/admin/knowledge/sources"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
NOT_AUTHENTICATED = {"detail": "Not authenticated."}


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


@dataclass
class Api:
    client: TestClient
    factory: Callable[[], AsyncSession]
    clock: Clock
    admin: User
    issuer: AdminTokens
    admin_emails: list[str]

    async def token(self, user: User | None = None, *, days: int = 30) -> str:
        return (await self.issuer.issue((user or self.admin).id, days=days)).secret

    def get(self, token: str | None, path: str = SOURCES) -> Response:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return self.client.get(path, headers=headers)


async def _user(factory: Callable[[], AsyncSession], **overrides: object) -> User:
    sub = f"api-{uuid4()}"
    fields: dict[str, object] = {
        "sub": sub,
        "email": f"{sub}@example.com",
        "name": sub,
        "locale": "en",
        "status": UserStatus.ACTIVE,
    }
    fields.update(overrides)
    return await SqlAlchemyUsersRepository(factory).create(NewUser.model_validate(fields))


async def _api(
    factory: Callable[[], AsyncSession],
    *,
    per_minute: int = 120,
    failures: int = 10,
    bucket: FakeBucket | None = None,
) -> Api:
    """The API with real Postgres; PMC's bucket is `bucket`'s mock transport, never the network."""
    users = SqlAlchemyUsersRepository(factory)
    sessions = SqlAlchemySessionsRepository(factory)
    tokens = SqlAlchemyAdminTokensRepository(factory)
    sources = SqlAlchemyKnowledgeSourcesRepository(factory)
    clock = Clock()
    admin = await _user(factory)
    admin_emails = [admin.email]
    templates = build_templates()
    app = FastAPI()
    app.include_router(
        build_api_router(
            authenticator=TokenAuthenticator(
                tokens=tokens, users=users, clock=clock, admin_emails=admin_emails
            ),
            knowledge=KnowledgeSources(
                sources=sources,
                storage=None,
                clock=clock,
                max_upload_bytes=1024,
                sports=["gym"],
            ),
            staging=KnowledgeStaging(
                sources=sources,
                pmc=PmcCloudMetadata(
                    bucket_url=BUCKET,
                    timeout_seconds=1.0,
                    transport=(bucket or FakeBucket()).transport(),
                ),
                clock=clock,
                sports=["gym"],
                # The test factory joins every session to one connection, whose savepoints
                # can't interleave; in the app each session has its own pooled connection.
                concurrency=1,
            ),
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=clock,
        templates=templates,
        admin_emails=admin_emails,
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    app.add_middleware(
        RateLimitMiddleware,
        clock=clock,
        templates=templates,
        auth_per_minute=10,
        session_per_minute=120,
        token_per_minute=per_minute,
        token_failures_per_address_per_minute=failures,
    )
    return Api(
        client=TestClient(app),
        factory=factory,
        clock=clock,
        admin=admin,
        issuer=AdminTokens(tokens=tokens, clock=clock),
        admin_emails=admin_emails,
    )


async def _add_source(api: Api, **overrides: object) -> NewKnowledgeSource:
    source_id = uuid4()
    fields: dict[str, object] = {
        "id": source_id,
        "title": "Deload timing in resistance training",
        "author": "M. Kowalska",
        "source_type": SourceType.RESEARCH_PAPER,
        "evidence_tier": EvidenceTier.PEER_REVIEWED,
        "language": SourceLanguage.EN,
        "sports": ("gym",),
        "topics": ("deload",),
        "license_note": None,
        "license_id": LicenseId.CC_BY_SA,
        "license_source": LicenseSource.PMC,
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "source_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
        "pmcid": "PMC1234567",
        "doi": "10.1000/Deload.1",
        "pmid": "7654321",
        "content_hash": content_hash(f"api-{source_id}".encode()),
        "file_type": KnowledgeFileType.JATS,
        "file_name": None,
        "size_bytes": None,
        "storage_key": None,
        "status": SourceStatus.PROPOSED,
        "uploaded_by_user_id": api.admin.id,
        "uploaded_at": NOW,
    }
    fields.update(overrides)
    source = NewKnowledgeSource.model_validate(fields)
    await SqlAlchemyKnowledgeSourcesRepository(api.factory).add(source)
    return source


async def _last_used(api: Api) -> list[datetime | None]:
    async with api.factory() as session:
        result = await session.execute(
            text("SELECT last_used_at FROM admin_tokens WHERE user_id = :id"), {"id": api.admin.id}
        )
        return [row[0] for row in result]


def _listed(body: Mapping[str, object], source_id: UUID) -> Mapping[str, object]:
    sources = body["sources"]
    assert isinstance(sources, list)
    [found] = [s for s in sources if s["id"] == str(source_id)]
    assert isinstance(found, dict)
    return found


async def _all_pages(api: Api, token: str, limit: int) -> list[str]:
    seen: list[str] = []
    after: str | None = None
    while True:
        query = f"?limit={limit}" + ("" if after is None else f"&after={after}")
        response = api.client.get(SOURCES + query, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["sources"]) <= limit
        seen.extend(s["id"] for s in body["sources"])
        after = body["next"]
        if after is None:
            return seen


async def test_a_valid_token_lists_sources_with_status_identifiers_licence_and_hash(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """AC1, and `last_used_at` is set (ADR-0036)."""
    api = await _api(db_session_factory)
    source = await _add_source(api)
    token = await api.token()
    api.clock.at = NOW + timedelta(minutes=5)

    response = api.client.get(SOURCES, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert _listed(response.json(), source.id) == {
        "id": str(source.id),
        "title": "Deload timing in resistance training",
        "status": "proposed",
        "file_type": "jats",
        "source_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
        "pmcid": "PMC1234567",
        "doi": "10.1000/deload.1",
        "pmid": "7654321",
        "isbn": None,
        "license_id": "cc-by-sa",
        "license_source": "pmc",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "licence_flag": "sa",
        "content_hash": source.content_hash,
    }
    assert await _last_used(api) == [NOW + timedelta(minutes=5)]


async def test_the_bearer_scheme_is_case_insensitive(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    api = await _api(db_session_factory)
    token = await api.token()

    response = api.client.get(SOURCES, headers={"Authorization": f"bearer {token}"})

    assert response.status_code == 200, response.text


async def test_the_list_pages_by_cursor(db_session_factory: Callable[[], AsyncSession]) -> None:
    """Owner, plan gate: `after` and `limit`, keyset on `id`."""
    api = await _api(db_session_factory)
    added = sorted(
        [str((await _add_source(api, pmcid=None, doi=None, pmid=None)).id) for _ in range(3)]
    )
    token = await api.token()

    seen = await _all_pages(api, token, limit=2)

    mine = [source_id for source_id in seen if source_id in added]
    assert mine == added
    assert len(seen) == len(set(seen))


@pytest.mark.parametrize("query", ["?limit=0", "?limit=501", "?after=not-a-uuid"])
async def test_a_bad_page_query_answers_422(
    db_session_factory: Callable[[], AsyncSession], query: str
) -> None:
    api = await _api(db_session_factory)
    token = await api.token()

    response = api.client.get(SOURCES + query, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 422


@pytest.mark.parametrize("case", ["expired", "revoked", "unknown", "malformed", "missing", "basic"])
async def test_an_expired_revoked_unknown_or_malformed_token_answers_the_same_401(
    db_session_factory: Callable[[], AsyncSession], case: str
) -> None:
    """AC2: one answer for every failure, never saying which."""
    api = await _api(db_session_factory)
    token = await api.token(days=1)
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    match case:
        case "expired":
            api.clock.at = NOW + timedelta(days=1)
        case "revoked":
            [issued] = await api.issuer.list(api.admin.id)
            await api.issuer.revoke(api.admin.id, issued.id)
        case "unknown":
            headers = {"Authorization": "Bearer atk_" + "Q" * 43}
        case "malformed":
            headers = {"Authorization": "Bearer not-a-token"}
        case "missing":
            headers = {}
        case "basic":
            headers = {"Authorization": "Basic YWRtaW46YWRtaW4="}
        case _:
            raise AssertionError(case)

    response = api.client.get(SOURCES, headers=headers)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert await _last_used(api) == [None]


@pytest.mark.parametrize("change", ["pending", "disabled", "not_admin"])
async def test_a_token_whose_user_lost_active_or_admin_answers_403(
    db_session_factory: Callable[[], AsyncSession], change: str
) -> None:
    """AC3, re-checked on the request after the change, with the same token."""
    api = await _api(db_session_factory)
    token = await api.token()
    assert api.get(token).status_code == 200
    if change == "not_admin":
        api.admin_emails.clear()
    else:
        async with api.factory() as session:
            await session.execute(
                text("UPDATE users SET status = :status WHERE id = :id"),
                {"status": change, "id": api.admin.id},
            )
            await session.commit()

    response = api.client.get(SOURCES, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json() == {"detail": "This token's account can't use the API."}


async def test_the_121st_request_in_a_minute_answers_429(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """AC4 through the app's own stack; `test_rate_limit.py` covers the window itself."""
    api = await _api(db_session_factory)
    token = await api.token()
    headers = {"Authorization": f"Bearer {token}"}

    statuses = [api.client.get(SOURCES, headers=headers).status_code for _ in range(121)]

    assert statuses == [200] * 120 + [429]


async def test_a_full_address_gets_429_with_a_valid_token_and_last_used_is_unchanged(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The failure limit answers before the token is read, so the database is not touched."""
    api = await _api(db_session_factory, failures=2)
    token = await api.token()
    before = await _last_used(api)

    bad = [
        api.client.get(SOURCES, headers={"Authorization": "Bearer atk_" + "z" * 43}).status_code
        for _ in range(2)
    ]
    refused = api.client.get(SOURCES, headers={"Authorization": f"Bearer {token}"})

    assert bad == [401, 401]
    assert refused.status_code == 429
    assert "Retry-After" in refused.headers
    assert await _last_used(api) == before


@pytest.mark.parametrize(
    "query",
    ["?access_token={t}", "?token={t}", "?Access_Token={t}", "?q={t}", "?{t}", "?limit={t}"],
    ids=["access_token", "token", "mixed-case", "any-value", "bare", "limit"],
)
async def test_a_token_in_the_query_string_is_refused(
    db_session_factory: Callable[[], AsyncSession], query: str
) -> None:
    """AC5: refused even beside a valid header, before the query is validated (so a 422 never
    echoes it), and the token is not used."""
    api = await _api(db_session_factory)
    token = await api.token()

    response = api.client.get(
        SOURCES + query.format(t=token), headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Send the token in the Authorization header only."}
    assert token not in response.text
    assert await _last_used(api) == [None]


async def test_a_session_cookie_alone_does_not_authenticate_the_route(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """AC6: a signed-in admin's live session, no bearer: 401 JSON, not the app's pages."""
    api = await _api(db_session_factory)
    session = await SqlAlchemySessionsRepository(api.factory).create(
        NewSession(user_id=api.admin.id, expires_at=NOW + timedelta(days=7))
    )
    api.client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = api.client.get(SOURCES)
    posted = api.client.post(SOURCES)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    # No CSRF page either: the prefix is outside the cookie's world altogether.
    assert posted.status_code in {401, 405}
    assert "<html" not in posted.text


async def test_another_admins_token_works_for_them_and_is_touched_alone(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Tenancy: a token is its own user's; touching it leaves the other admin's alone."""
    api = await _api(db_session_factory)
    other = await _user(db_session_factory)
    api.admin_emails.append(other.email)
    mine = await api.token()
    theirs = await api.token(other)

    assert api.get(theirs).status_code == 200

    assert await _last_used(api) == [None]
    assert api.get(mine).status_code == 200
