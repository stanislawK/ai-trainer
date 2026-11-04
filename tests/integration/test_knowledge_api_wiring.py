"""The knowledge API through the real composition root (ADR-0036, #457): routed, gated by the
token alone, and the token never in a log line, a span or a response body. The admin and the
token are committed, because `create_app` opens its own engine; deleting the user at teardown
cascades to the token."""

import io
import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from sqlalchemy import text

from ai_trainer.adapters.admin_token_repository import SqlAlchemyAdminTokensRepository
from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.admin_tokens import AdminTokens
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.main import create_app
from ai_trainer.settings import Settings
from tests.integration.test_request_logs import log_lines
from tests.integration.test_request_tracing import UNREACHABLE_ENDPOINT, _finished
from tests.knowledge_support import pdf_bytes
from tests.object_storage_support import (
    ACCESS_KEY_ID,
    BUCKET,
    SECRET_ACCESS_KEY,
    S3State,
    put_via_link,
    s3_server,
)

__all__ = ["log_lines"]

SOURCES = "/api/admin/knowledge/sources"


@pytest.fixture
async def admin_token() -> AsyncIterator[tuple[User, str]]:
    engine = build_engine(str(Settings().database_url))
    factory = build_session_factory(engine)
    users = SqlAlchemyUsersRepository(factory)
    sub = f"api-wiring-{uuid4()}"
    user = await users.create(
        NewUser(
            sub=sub,
            email=f"{sub}@example.com",
            name="API Wiring",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    issued = await AdminTokens(
        tokens=SqlAlchemyAdminTokensRepository(factory), clock=UtcClock()
    ).issue(user.id, days=1)
    try:
        yield user, issued.secret
    finally:
        await users.delete(user.id)
        await engine.dispose()


def _client(
    exporter: InMemorySpanExporter,
    user: User,
    *,
    admin: bool = True,
    storage: S3State | None = None,
) -> TestClient:
    settings = Settings(
        otel_enabled=True,
        otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT,
        admin_emails=[user.email] if admin else [],
        token_requests_per_minute=3,
    )
    if storage is not None:
        settings = settings.model_copy(
            update={
                "object_storage_endpoint": storage.endpoint,
                "object_storage_access_key_id": SecretStr(ACCESS_KEY_ID),
                "object_storage_secret_access_key": SecretStr(SECRET_ACCESS_KEY),
                "object_storage_uploads_bucket": BUCKET,
            }
        )
    return TestClient(
        create_app(settings, span_exporter=exporter, metric_reader=InMemoryMetricReader())
    )


async def test_the_app_serves_the_list_to_a_token_and_never_records_it(
    admin_token: tuple[User, str], log_lines: io.StringIO
) -> None:
    user, token = admin_token
    exporter = InMemorySpanExporter()
    client = _client(exporter, user)
    bearer = {"Authorization": f"Bearer {token}"}

    responses = [
        client.get(SOURCES, headers=bearer),
        client.get(f"{SOURCES}?access_token={token}", headers=bearer),
        client.get(SOURCES, headers={"Authorization": f"Bearer {token}x"}),
        client.get(SOURCES, headers=bearer),
        client.get(SOURCES, headers=bearer),
    ]

    assert [r.status_code for r in responses] == [200, 400, 401, 200, 429]
    assert "sources" in responses[0].json()
    spans = _finished(exporter)
    assert spans
    exported = json.dumps(
        [
            [s.name, s.status.description, dict(s.attributes or {})]
            + [dict(e.attributes or {}) for e in s.events]
            for s in spans
        ],
        default=str,
    )
    for secret in (token, token[4:]):
        assert secret not in exported
        assert secret not in log_lines.getvalue()
        for response in responses:
            assert secret not in response.text
            assert secret not in json.dumps(dict(response.headers))
    assert '"route": "/api/admin/knowledge/sources"' in log_lines.getvalue()


async def test_the_app_routes_pmc_staging_past_csrf_with_the_registrys_sports(
    admin_token: tuple[User, str],
) -> None:
    """A POST with a bearer token and no CSRF token reaches the route (#458), whose sports are the
    registry's. An unknown sport is refused before PMC is asked, so no request leaves the test."""
    user, token = admin_token
    client = _client(InMemorySpanExporter(), user)
    article = {"pmcid": "PMC1", "evidence_tier": "peer_reviewed", "sports": ["chess"]}

    response = client.post(
        "/api/admin/knowledge/stage/pmc",
        json={"articles": [article]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Name only the app's sports: chess."}


async def test_the_app_stages_a_file_with_the_uploads_bucket_and_never_records_the_link(
    admin_token: tuple[User, str], log_lines: io.StringIO
) -> None:
    """Stage, PUT and register through the composition root (#460): past CSRF on a bearer token,
    with the `uploads` bucket from `OBJECT_STORAGE_*`. The link is in the stage's answer and
    nowhere else: no log line, no span."""
    user, token = admin_token
    exporter = InMemorySpanExporter()
    bearer = {"Authorization": f"Bearer {token}"}
    source = {
        "title": "Wiring check",
        "language": "en",
        "evidence_tier": "coaching_book",
        "source_type": "book",
        "sports": ["gym"],
        "licence": None,
    }
    with s3_server() as storage:
        client = _client(exporter, user, storage=storage)
        staged = client.post(
            "/api/admin/knowledge/stage/file",
            json={"file_name": "wiring.pdf", "source": source},
            headers=bearer,
        ).json()
        assert put_via_link(staged["put_url"], pdf_bytes(f"wiring-{uuid4()}")) == 200
        registered = client.post(
            "/api/admin/knowledge/register",
            json={"upload_id": staged["upload_id"]},
            headers=bearer,
        )

    engine = build_engine(str(Settings().database_url))
    async with build_session_factory(engine)() as session:
        await session.execute(
            text("DELETE FROM knowledge_sources WHERE id = :id"), {"id": staged["upload_id"]}
        )
        await session.commit()
    await engine.dispose()
    assert registered.status_code == 200, registered.text
    assert registered.json()["outcome"] == "registered"
    signature = parse_qs(urlsplit(staged["put_url"]).query)["X-Amz-Signature"][0]
    exported = json.dumps(
        [
            [s.name, dict(s.attributes or {})] + [dict(e.attributes or {}) for e in s.events]
            for s in _finished(exporter)
        ],
        default=str,
    )
    for secret in (signature, ACCESS_KEY_ID):
        assert secret not in exported
        assert secret not in log_lines.getvalue()


async def test_the_app_answers_403_once_the_user_leaves_admin_emails(
    admin_token: tuple[User, str],
) -> None:
    user, token = admin_token
    client = _client(InMemorySpanExporter(), user, admin=False)

    response = client.get(SOURCES, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
