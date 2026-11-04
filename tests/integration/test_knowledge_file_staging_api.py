"""Staging files for upload by presigned PUT through the knowledge API (ADR-0036, #460): real
PostgreSQL, the app's middleware, the adapter on the small S3 server in object_storage_support.py,
and the bytes PUT to the link as `curl -T` would.

Every file is unique per test: `content_hash` is unique, and a second insert of a hash another
test's open transaction holds would wait for it (knowledge_support.py)."""

import logging
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.object_storage import ObstoreObjectStorage
from ai_trainer.application.knowledge_file_staging import staged_key, upload_key
from ai_trainer.domain.knowledge import (
    KnowledgeFileType,
    SourceStatus,
    content_hash,
)
from tests.integration.test_knowledge_api import SOURCES, Api, _add_source, _api, _user
from tests.knowledge_support import pdf_bytes
from tests.object_storage_support import (
    ACCESS_KEY_ID,
    BUCKET,
    SECRET_ACCESS_KEY,
    S3State,
    put_via_link,
    s3_server,
)

STAGE = "/api/admin/knowledge/stage/file"
REGISTER = "/api/admin/knowledge/register"
MAX_BYTES = 1024


@pytest.fixture
def server() -> Iterator[S3State]:
    with s3_server() as state:
        yield state


async def _files_api(factory: Callable[[], AsyncSession], server: S3State) -> Api:
    storage = ObstoreObjectStorage(
        endpoint=server.endpoint,
        region="auto",
        access_key_id=ACCESS_KEY_ID,
        secret_access_key=SECRET_ACCESS_KEY,
        bucket=BUCKET,
        timeout_seconds=5,
    )
    return await _api(factory, storage=storage, max_upload_bytes=MAX_BYTES)


def _book(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "type": "book",
        "title": "Science and Practice of Strength Training",
        "author": [{"family": "Zatsiorsky", "given": "Vladimir"}],
        "ISBN": f"978{uuid4().int % 10**10:010d}",
        "language": "en",
        "evidence_tier": "coaching_book",
        "source_type": "book",
        "sports": ["gym"],
        "licence": {
            "license_id": "other",
            "license_source": "admin",
            "attribution": "Zatsiorsky, Kraemer, Fry, Human Kinetics 2021",
            "license_note": "The owner's own copy.",
        },
    }
    fields.update(overrides)
    return fields


def _post(api: Api, token: str | None, path: str, body: object) -> Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return api.client.post(path, json=body, headers=headers)


def _stage(api: Api, token: str, file_name: str = "zatsiorsky.pdf", **fields: object) -> Any:
    response = _post(api, token, STAGE, {"file_name": file_name, "source": _book(**fields)})
    assert response.status_code == 200, response.text
    return response.json()


def _register(api: Api, token: str, upload_id: str) -> Response:
    return _post(api, token, REGISTER, {"upload_id": upload_id})


async def test_stage_put_register_leaves_a_proposed_source_with_its_type_and_size(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    data = pdf_bytes(f"stage-put-register-{uuid4()}")

    staged = _stage(api, token)
    assert staged["outcome"] == "ready"
    assert put_via_link(staged["put_url"], data) == 200
    response = _register(api, token, staged["upload_id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "registered"
    assert body["source_id"] == staged["upload_id"]
    assert body["status"] == SourceStatus.PROPOSED
    assert body["file_type"] == KnowledgeFileType.PDF
    assert body["size_bytes"] == len(data)
    listed = {s["id"]: s for s in api.get(token, f"{SOURCES}?limit=500").json()["sources"]}
    row = listed[staged["upload_id"]]
    assert row["status"] == SourceStatus.PROPOSED
    assert row["file_type"] == KnowledgeFileType.PDF
    assert row["content_hash"] == content_hash(data)
    assert server.objects[upload_key(staged["upload_id"])] == data
    assert staged_key(staged["upload_id"]) not in server.objects


async def test_registering_a_file_already_ingested_reports_it_and_deletes_the_new_object(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    data = pdf_bytes(f"already-ingested-{uuid4()}")
    ingested = await _add_source(
        api,
        pmcid=None,
        doi=None,
        pmid=None,
        content_hash=content_hash(data),
        status=SourceStatus.INGESTED,
    )
    staged = _stage(api, token)
    put_via_link(staged["put_url"], data)

    body = _register(api, token, staged["upload_id"]).json()

    assert body["outcome"] == "covered"
    assert body["source_id"] == str(ingested.id)
    assert body["status"] == SourceStatus.INGESTED
    assert upload_key(staged["upload_id"]) not in server.objects
    assert staged_key(staged["upload_id"]) not in server.objects


@pytest.mark.parametrize(
    ("data", "reason"),
    [(b"PK\x03\x04 a zip, not a pdf", "unlisted_type"), (b"%PDF-" + b"x" * MAX_BYTES, "too_large")],
    ids=["wrong_type", "too_large"],
)
async def test_a_wrong_type_or_oversized_object_is_refused_and_deleted(
    db_session_factory: Callable[[], AsyncSession], server: S3State, data: bytes, reason: str
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    staged = _stage(api, token)
    put_via_link(staged["put_url"], data + uuid4().bytes)

    body = _register(api, token, staged["upload_id"]).json()

    assert body == {
        "outcome": "refused",
        "reason": reason,
        "source_id": None,
        "status": None,
        "file_type": None,
        "size_bytes": None,
    }
    assert upload_key(staged["upload_id"]) not in server.objects
    assert staged_key(staged["upload_id"]) not in server.objects
    assert _register(api, token, staged["upload_id"]).status_code == 404


async def test_registering_before_the_put_reports_the_object_missing(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    staged = _stage(api, token)

    body = _register(api, token, staged["upload_id"]).json()

    assert body["outcome"] == "missing"
    assert body["source_id"] is None
    put_via_link(staged["put_url"], pdf_bytes(f"late-put-{uuid4()}"))
    assert _register(api, token, staged["upload_id"]).json()["outcome"] == "registered"


async def test_an_expired_link_is_refused_and_staging_again_issues_a_new_one(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    first = _stage(api, token)

    server.seconds_after_signing = 3601
    assert put_via_link(first["put_url"], pdf_bytes(f"expired-{uuid4()}")) == 403
    assert upload_key(first["upload_id"]) not in server.objects

    # A new stage signs a new link at a new time.
    server.seconds_after_signing = 0
    second = _stage(api, token)
    assert second["upload_id"] != first["upload_id"]
    assert second["put_url"] != first["put_url"]
    assert put_via_link(second["put_url"], pdf_bytes(f"restaged-{uuid4()}")) == 200
    assert _register(api, token, second["upload_id"]).json()["outcome"] == "registered"


async def test_a_stage_for_a_covered_isbn_issues_no_link(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    isbn = f"978{uuid4().int % 10**10:010d}"
    here = await _add_source(api, pmcid=None, doi=None, pmid=None, isbn=isbn)

    body = _stage(api, token, ISBN=isbn)

    assert body["outcome"] == "covered"
    assert body["source_id"] == str(here.id)
    assert body["put_url"] is None
    assert server.objects == {}


async def test_an_invalid_template_is_refused_per_field(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()

    response = _post(
        api,
        token,
        STAGE,
        {"file_name": "notes.docx", "source": _book(sports=["rowing"], licence=None, title="")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "file_name": "unlisted_type",
        "title": "string_too_short",
        "sports": "unknown",
    }
    assert server.objects == {}


async def test_registering_an_upload_no_stage_issued_is_not_found(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()

    assert _register(api, token, str(uuid4())).status_code == 404


async def test_without_storage_settings_both_routes_answer_503(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    api = await _api(db_session_factory)
    token = await api.token()

    staged = _post(api, token, STAGE, {"file_name": "a.pdf", "source": _book()})
    registered = _register(api, token, str(uuid4()))

    assert staged.status_code == registered.status_code == 503
    assert staged.json()["detail"] == "not_configured"


async def test_a_storage_failure_answers_503(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    server.refuse_with = 500

    response = _post(api, token, STAGE, {"file_name": "a.pdf", "source": _book()})

    assert response.status_code == 503
    assert response.json()["detail"] == "failed"


async def test_the_file_routes_need_an_admin_token_and_take_no_file_bytes(
    db_session_factory: Callable[[], AsyncSession], server: S3State
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()
    stranger = await api.token(await _user(api.factory))

    assert _post(api, None, STAGE, {"file_name": "a.pdf", "source": _book()}).status_code == 401
    assert _register(api, stranger, str(uuid4())).status_code == 403
    files = {"file": ("a.pdf", pdf_bytes("multipart"), "application/pdf")}
    multipart = api.client.post(STAGE, files=files, headers={"Authorization": f"Bearer {token}"})
    assert multipart.status_code == 422
    assert server.objects == {}


async def test_no_link_reaches_the_logs(
    db_session_factory: Callable[[], AsyncSession],
    server: S3State,
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = await _files_api(db_session_factory, server)
    token = await api.token()

    with caplog.at_level(logging.DEBUG):
        staged = _stage(api, token)
        put_via_link(staged["put_url"], pdf_bytes(f"logs-{uuid4()}"))
        _register(api, token, staged["upload_id"])

    assert "X-Amz-Signature" not in caplog.text
    assert ACCESS_KEY_ID not in caplog.text
