"""`ai-trainer ingest-worker --once` wired end to end (ADR-0036, #423): real PostgreSQL, an S3
server on 127.0.0.1 and OpenRouter faked at the HTTP transport. The command opens its own engine
and commits, so the source is a real row, dated 2002 so it is claimed before any dev data but after
`test_knowledge_ingestion`'s 2001 sources, and removed after."""

import io
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import text

from ai_trainer import cli
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.settings import Settings
from tests.ingestion_support import DIMENSIONS, guide_markdown, uploaded_source
from tests.object_storage_support import (
    ACCESS_KEY_ID,
    BUCKET,
    SECRET_ACCESS_KEY,
    S3State,
    s3_server,
)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3State]:
    with s3_server() as state:
        monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT", state.endpoint)
        monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY_ID", ACCESS_KEY_ID)
        monkeypatch.setenv("OBJECT_STORAGE_SECRET_ACCESS_KEY", SECRET_ACCESS_KEY)
        monkeypatch.setenv("OBJECT_STORAGE_UPLOADS_BUCKET", BUCKET)
        monkeypatch.setenv("OBJECT_STORAGE_TIMEOUT_SECONDS", "5")
        monkeypatch.setenv("KNOWLEDGE_CHUNK_MAX_WORDS", "40")
        monkeypatch.setenv("KNOWLEDGE_CHUNK_MIN_WORDS", "5")
        yield state


@pytest.fixture
async def source_id(server: S3State) -> AsyncIterator[UUID]:
    engine = build_engine(str(Settings().database_url))
    factory = build_session_factory(engine)
    admin = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub="zz-cli-ingest-admin",
            email="zz-cli-ingest-admin@example.com",
            name="Admin",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    new = uploaded_source(
        admin_id=admin.id, marker="zz-cli", uploaded_at=datetime(2002, 1, 1, tzinfo=UTC)
    )
    assert new.storage_key is not None
    server.objects[new.storage_key] = guide_markdown("zz-cli")
    await SqlAlchemyKnowledgeSourcesRepository(factory).add(new)
    try:
        yield new.id
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM knowledge_sources WHERE id = :id"), {"id": new.id}
            )
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin.id})
        await engine.dispose()


def _openrouter(seen: list[dict[str, object]]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        inputs = body["input"]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": body["model"],
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.5] * DIMENSIONS}
                    for i in range(len(inputs))
                ],
                "usage": {"prompt_tokens": 40, "total_tokens": 40, "cost": 0.0000004},
            },
        )

    return httpx2.MockTransport(handler)


async def test_once_ingests_the_uploaded_source_and_accounts_each_call(source_id: UUID) -> None:
    seen: list[dict[str, object]] = []
    output = io.StringIO()

    code = await cli.run(
        [cli.INGEST_WORKER, "--once"], output=output, embedding_transport=_openrouter(seen)
    )

    assert code == 0
    lines = output.getvalue().splitlines()
    assert f"ingest-worker: source={source_id} result=ingested chunks=3" in lines
    assert lines[-1].startswith("ingest-worker: result=ok sources=")
    assert seen[0]["provider"] == {"only": ["deepinfra"]}
    engine = build_engine(str(Settings().database_url))
    try:
        async with engine.connect() as connection:
            chunks = await connection.scalar(
                text("SELECT count(*) FROM knowledge_chunks WHERE source_id = :id"),
                {"id": source_id},
            )
            calls = await connection.scalar(
                text(
                    "SELECT count(*) FROM llm_calls c JOIN knowledge_sources s"
                    " ON s.uploaded_by_user_id = c.user_id"
                    " WHERE s.id = :id AND c.template_id = 'knowledge_embedding'"
                ),
                {"id": source_id},
            )
    finally:
        await engine.dispose()
    assert chunks == 3
    # Batches of 32 by default: three chunks are one call, one row.
    assert calls == 1


async def test_without_storage_settings_it_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "OBJECT_STORAGE_ENDPOINT",
        "OBJECT_STORAGE_ACCESS_KEY_ID",
        "OBJECT_STORAGE_SECRET_ACCESS_KEY",
        "OBJECT_STORAGE_UPLOADS_BUCKET",
    ):
        monkeypatch.setenv(key, "")
    output = io.StringIO()

    code = await cli.run([cli.INGEST_WORKER, "--once"], output=output)

    assert code == 1
    assert output.getvalue() == "ingest-worker: result=failed reason=not_configured\n"
