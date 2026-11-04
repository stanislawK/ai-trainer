"""The obstore adapter against an S3 server on 127.0.0.1 (ADR-0033, ticket #420)."""

import logging
from collections.abc import Iterator
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from ai_trainer.adapters.object_storage import ObstoreObjectStorage, UrllibPresignedLinks
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError
from tests.object_storage_support import (
    ACCESS_KEY_ID,
    BUCKET,
    SECRET_ACCESS_KEY,
    S3State,
    put_via_link,
    s3_server,
    unused_endpoint,
)

KEY = "sources/plan.pdf"
LINK_TIME = timedelta(seconds=60)


def _storage(endpoint: str) -> ObstoreObjectStorage:
    return ObstoreObjectStorage(
        endpoint=endpoint,
        region="auto",
        access_key_id=ACCESS_KEY_ID,
        secret_access_key=SECRET_ACCESS_KEY,
        bucket=BUCKET,
        timeout_seconds=5,
    )


@pytest.fixture
def server() -> Iterator[S3State]:
    with s3_server() as state:
        yield state


async def test_put_then_get_returns_the_bytes(server: S3State) -> None:
    storage = _storage(server.endpoint)

    await storage.put(KEY, b"content", content_type="application/pdf")

    assert await storage.get(KEY) == b"content"
    assert server.content_types[KEY] == "application/pdf"


async def test_requests_are_signed_with_the_configured_key_and_region(server: S3State) -> None:
    await _storage(server.endpoint).put(KEY, b"content", content_type="text/plain")

    assert f"Credential={ACCESS_KEY_ID}/" in server.authorizations[0]
    assert "/auto/s3/aws4_request" in server.authorizations[0]


async def test_ambient_aws_environment_does_not_change_the_requests(
    server: S3State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-session-token")
    monkeypatch.setenv("AWS_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    storage = _storage(server.endpoint)

    await storage.put(KEY, b"content", content_type="text/plain")
    link = await storage.presigned_get(KEY, expires_in=LINK_TIME)

    assert "/auto/s3/aws4_request" in server.authorizations[0]
    assert link.startswith(server.endpoint)
    assert "ambient-session-token" not in link
    assert "X-Amz-Security-Token" not in link


async def test_ambient_aws_environment_cannot_turn_signing_off(
    server: S3State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SKIP_SIGNATURE", "true")
    monkeypatch.setenv("AWS_UNSIGNED_PAYLOAD", "true")
    monkeypatch.setenv("AWS_DISABLE_BULK_DELETE", "false")
    storage = _storage(server.endpoint)

    await storage.put(KEY, b"content", content_type="text/plain")
    await storage.delete(KEY)

    assert all(
        authorization.startswith("AWS4-HMAC-SHA256") for authorization in server.authorizations
    )
    assert len(server.authorizations) == 2
    assert server.objects == {}


async def test_a_content_type_that_cannot_be_a_header_is_refused(server: S3State) -> None:
    with pytest.raises(ObjectStorageError) as excinfo:
        await _storage(server.endpoint).put(KEY, b"content", content_type="text/plain\nx: y")

    assert excinfo.value.operation == "put"
    assert excinfo.value.reason == "invalid_content_type"
    assert server.objects == {}


async def test_get_of_a_missing_object_raises_not_found(server: S3State) -> None:
    with pytest.raises(ObjectNotFoundError) as excinfo:
        await _storage(server.endpoint).get(KEY)

    assert excinfo.value.operation == "get"
    assert excinfo.value.reason == "not_found"


async def test_delete_removes_the_object(server: S3State) -> None:
    storage = _storage(server.endpoint)
    await storage.put(KEY, b"content", content_type="text/plain")

    await storage.delete(KEY)

    assert server.objects == {}


async def test_deleting_a_missing_object_is_not_a_failure(server: S3State) -> None:
    await _storage(server.endpoint).delete(KEY)

    assert server.objects == {}


async def test_a_refused_delete_raises_a_domain_error(server: S3State) -> None:
    server.refuse_with = 403

    with pytest.raises(ObjectStorageError) as excinfo:
        await _storage(server.endpoint).delete(KEY)

    assert excinfo.value.operation == "delete"
    assert excinfo.value.reason == "denied"


async def test_presigned_link_expires_after_its_set_time(server: S3State) -> None:
    storage = _storage(server.endpoint)
    links = UrllibPresignedLinks(timeout_seconds=5)
    await storage.put(KEY, b"content", content_type="text/plain")

    link = await storage.presigned_get(KEY, expires_in=LINK_TIME)

    assert parse_qs(urlsplit(link).query)["X-Amz-Expires"] == ["60"]
    assert await links.status(link) == 200
    server.seconds_after_signing = 60
    assert await links.status(link) == 200
    server.seconds_after_signing = 61
    assert await links.status(link) == 403


@pytest.mark.parametrize(
    "expires_in",
    [timedelta(0), timedelta(days=7, seconds=1)],
    ids=["zero", "past-seven-days"],
)
async def test_presigned_link_refuses_a_time_r2_would_refuse(
    server: S3State, expires_in: timedelta
) -> None:
    with pytest.raises(ValueError, match="between"):
        await _storage(server.endpoint).presigned_get(KEY, expires_in=expires_in)


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "denied"), (500, "unavailable")],
    ids=["refused", "server-error"],
)
async def test_failures_raise_domain_errors_and_log_no_key(
    server: S3State, caplog: pytest.LogCaptureFixture, status: int, reason: str
) -> None:
    server.refuse_with = status
    storage = _storage(server.endpoint)

    with caplog.at_level(logging.DEBUG), pytest.raises(ObjectStorageError) as excinfo:
        await storage.get(KEY)

    assert excinfo.value.operation == "get"
    assert excinfo.value.reason == reason
    _assert_no_key(excinfo.value, caplog.text)
    assert "object storage get failed" in caplog.text


async def test_unreachable_service_raises_a_domain_error_and_logs_no_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = _storage(unused_endpoint())

    with caplog.at_level(logging.DEBUG), pytest.raises(ObjectStorageError) as excinfo:
        await storage.put(KEY, b"content", content_type="text/plain")

    assert excinfo.value.reason == "unavailable"
    _assert_no_key(excinfo.value, caplog.text)


async def test_invalid_key_raises_a_domain_error(server: S3State) -> None:
    with pytest.raises(ObjectStorageError) as excinfo:
        await _storage(server.endpoint).get("../outside")

    assert excinfo.value.reason == "invalid_key"


async def test_a_link_for_an_invalid_key_raises_a_domain_error(server: S3State) -> None:
    with pytest.raises(ObjectStorageError) as excinfo:
        await _storage(server.endpoint).presigned_get("../outside", expires_in=LINK_TIME)

    assert excinfo.value.operation == "presign"
    assert excinfo.value.reason == "invalid_key"


async def test_opening_a_link_on_a_dead_service_raises_a_domain_error() -> None:
    with pytest.raises(ObjectStorageError) as excinfo:
        await UrllibPresignedLinks(timeout_seconds=1).status(f"{unused_endpoint()}/x?sig=abc")

    assert excinfo.value.operation == "link"
    assert excinfo.value.reason == "unavailable"
    assert "sig=abc" not in str(excinfo.value)


async def test_a_link_answered_with_garbage_raises_a_domain_error(server: S3State) -> None:
    server.answer_garbage = True

    with pytest.raises(ObjectStorageError) as excinfo:
        await UrllibPresignedLinks(timeout_seconds=5).status(f"{server.endpoint}/x?sig=abc")

    assert excinfo.value.operation == "link"
    assert excinfo.value.reason == "unavailable"


async def test_opening_a_link_logs_nothing_of_it(
    server: S3State, caplog: pytest.LogCaptureFixture
) -> None:
    storage = _storage(server.endpoint)
    await storage.put(KEY, b"content", content_type="text/plain")

    with caplog.at_level(logging.DEBUG):
        link = await storage.presigned_get(KEY, expires_in=LINK_TIME)
        await UrllibPresignedLinks(timeout_seconds=5).status(link)

    assert "X-Amz-Signature" not in caplog.text
    assert ACCESS_KEY_ID not in caplog.text


def _assert_no_key(error: ObjectStorageError, logs: str) -> None:
    for text in (str(error), repr(error), logs):
        assert ACCESS_KEY_ID not in text
        assert SECRET_ACCESS_KEY not in text
        assert KEY not in text
    assert error.__cause__ is None
    assert error.__suppress_context__


async def test_a_presigned_put_stores_the_bytes_sent_to_it(server: S3State) -> None:
    storage = _storage(server.endpoint)

    link = await storage.presigned_put(KEY, expires_in=LINK_TIME)

    assert parse_qs(urlsplit(link).query)["X-Amz-Expires"] == ["60"]
    assert put_via_link(link, b"%PDF-1.7 staged") == 200
    assert await storage.get(KEY) == b"%PDF-1.7 staged"


async def test_an_expired_presigned_put_is_refused_by_storage(server: S3State) -> None:
    storage = _storage(server.endpoint)
    link = await storage.presigned_put(KEY, expires_in=LINK_TIME)

    server.seconds_after_signing = 61

    assert put_via_link(link, b"late") == 403
    assert KEY not in server.objects


@pytest.mark.parametrize(
    "expires_in",
    [timedelta(0), timedelta(days=7, seconds=1)],
    ids=["zero", "past-seven-days"],
)
async def test_a_presigned_put_refuses_a_time_r2_would_refuse(
    server: S3State, expires_in: timedelta
) -> None:
    with pytest.raises(ValueError, match="between"):
        await _storage(server.endpoint).presigned_put(KEY, expires_in=expires_in)


async def test_a_put_link_for_an_invalid_key_raises_a_domain_error(server: S3State) -> None:
    with pytest.raises(ObjectStorageError) as excinfo:
        await _storage(server.endpoint).presigned_put("../outside", expires_in=LINK_TIME)

    assert excinfo.value.operation == "presign"
    assert excinfo.value.reason == "invalid_key"


async def test_signing_a_put_link_logs_nothing_of_it(
    server: S3State, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        link = await _storage(server.endpoint).presigned_put(KEY, expires_in=LINK_TIME)
        put_via_link(link, b"content")

    assert "X-Amz-Signature" not in caplog.text
    assert ACCESS_KEY_ID not in caplog.text


async def test_size_reads_an_objects_length_without_its_bytes(server: S3State) -> None:
    storage = _storage(server.endpoint)
    await storage.put(KEY, b"12345", content_type="text/plain")
    server.gets = 0

    assert await storage.size(KEY) == 5
    assert server.gets == 0


async def test_size_of_a_missing_object_raises_not_found(server: S3State) -> None:
    with pytest.raises(ObjectNotFoundError) as excinfo:
        await _storage(server.endpoint).size("sources/absent")

    assert excinfo.value.operation == "head"


async def test_a_refused_head_raises_a_domain_error(server: S3State) -> None:
    server.refuse_with = 403

    with pytest.raises(ObjectStorageError) as excinfo:
        await _storage(server.endpoint).size(KEY)

    assert excinfo.value.reason == "denied"
