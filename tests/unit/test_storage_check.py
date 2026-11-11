"""The storage round trip with fakes (ADR-0033, ticket #420)."""

from datetime import timedelta

import pytest

from ai_trainer.application.storage_check import (
    LINK_SECONDS,
    WAIT_SECONDS,
    StorageCheckFailure,
    check_media_public,
    check_storage,
)
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError

KEY = "storage-check/probe"
PLAIN_URL = "http://storage/uploads/storage-check/probe"
PUBLIC_URL = "https://media.example.com/storage-check/media-probe"
MEDIA_KEY = "storage-check/media-probe"


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.failing: set[str] = set()
        self.corrupt = False
        self.keep_on_delete = False
        self.link_times: list[timedelta] = []

    def _record(self, operation: str) -> None:
        self.calls.append(operation)
        if operation in self.failing:
            raise ObjectStorageError("put", "denied")

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self._record("put")
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        self._record("get")
        if key not in self.objects:
            raise ObjectNotFoundError("get")
        return b"something else" if self.corrupt else self.objects[key]

    async def size(self, key: str) -> int:
        raise AssertionError("storage-check reads no sizes")

    async def presigned_put(self, key: str, *, expires_in: timedelta) -> str:
        raise AssertionError("storage-check makes no upload links")

    async def delete(self, key: str) -> None:
        self._record("delete")
        if not self.keep_on_delete:
            self.objects.pop(key, None)

    async def presigned_get(self, key: str, *, expires_in: timedelta) -> str:
        self._record("presigned_get")
        self.link_times.append(expires_in)
        return "http://storage/link"


class FakeLinks:
    """Answers `statuses` in turn."""

    def __init__(self, *statuses: int | ObjectStorageError) -> None:
        self.statuses = list(statuses)
        self.urls: list[str] = []

    async def status(self, link: str) -> int:
        self.urls.append(link)
        answer = self.statuses.pop(0)
        if isinstance(answer, ObjectStorageError):
            raise answer
        return answer


class FakeSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


async def _check(
    storage: FakeStorage, links: FakeLinks, sleep: FakeSleep | None = None
) -> StorageCheckFailure | None:
    return await check_storage(
        storage=storage,
        links=links,
        sleep=sleep or FakeSleep(),
        probe_key=KEY,
        plain_url=PLAIN_URL,
    )


async def test_check_puts_reads_back_and_deletes() -> None:
    storage = FakeStorage()

    assert await _check(storage, FakeLinks(403, 200, 403)) is None

    assert storage.calls == ["put", "get", "presigned_get", "delete", "get"]
    assert storage.objects == {}


async def test_check_waits_past_the_link_time_before_trying_it_again() -> None:
    storage = FakeStorage()
    sleep = FakeSleep()

    await _check(storage, FakeLinks(403, 200, 403), sleep)

    assert storage.link_times == [timedelta(seconds=LINK_SECONDS)]
    assert sleep.waits == [WAIT_SECONDS]
    assert WAIT_SECONDS > LINK_SECONDS


async def test_check_fails_when_the_put_fails() -> None:
    storage = FakeStorage()
    storage.failing = {"put"}

    assert await _check(storage, FakeLinks()) == StorageCheckFailure(
        step="put", reason="denied", probe_left=False
    )
    assert storage.calls == ["put"]


async def test_check_fails_when_the_bytes_differ_and_still_deletes() -> None:
    storage = FakeStorage()
    storage.corrupt = True

    assert await _check(storage, FakeLinks(403)) == StorageCheckFailure(
        step="read", reason="mismatch", probe_left=False
    )
    assert storage.objects == {}


async def test_check_fails_when_a_fresh_link_is_refused() -> None:
    storage = FakeStorage()

    assert await _check(storage, FakeLinks(403, 403)) == StorageCheckFailure(
        step="link", reason="status_403", probe_left=False
    )
    assert storage.objects == {}


async def test_check_fails_when_a_link_cannot_be_opened() -> None:
    storage = FakeStorage()

    assert await _check(
        storage, FakeLinks(403, ObjectStorageError("link", "unavailable"))
    ) == StorageCheckFailure(step="link", reason="unavailable", probe_left=False)


async def test_check_fails_when_a_link_outlives_its_time() -> None:
    storage = FakeStorage()

    assert await _check(storage, FakeLinks(403, 200, 200)) == StorageCheckFailure(
        step="expiry", reason="status_200", probe_left=False
    )
    assert storage.objects == {}


@pytest.mark.parametrize("status", [404, 429, 500])
async def test_check_takes_only_a_refusal_for_an_expired_link(status: int) -> None:
    assert await _check(FakeStorage(), FakeLinks(403, 200, status)) == StorageCheckFailure(
        step="expiry", reason=f"status_{status}", probe_left=False
    )


@pytest.mark.parametrize("status", [400, 403])
async def test_check_accepts_the_statuses_services_refuse_an_expired_link_with(
    status: int,
) -> None:
    assert await _check(FakeStorage(), FakeLinks(403, 200, status)) is None


async def test_check_fails_when_the_delete_fails() -> None:
    storage = FakeStorage()
    storage.failing = {"delete"}

    assert await _check(storage, FakeLinks(403, 200, 403)) == StorageCheckFailure(
        step="delete", reason="denied", probe_left=True
    )


async def test_check_fails_when_the_object_survives_the_delete() -> None:
    storage = FakeStorage()
    storage.keep_on_delete = True

    assert await _check(storage, FakeLinks(403, 200, 403)) == StorageCheckFailure(
        step="delete", reason="still_there", probe_left=True
    )


async def test_check_reports_a_probe_it_could_not_clean_up() -> None:
    storage = FakeStorage()
    storage.corrupt = True
    storage.failing = {"delete"}

    assert await _check(storage, FakeLinks(403)) == StorageCheckFailure(
        step="read", reason="mismatch", probe_left=True
    )


async def test_check_asks_for_the_object_without_credentials_first() -> None:
    links = FakeLinks(403, 200, 403)

    await _check(FakeStorage(), links)

    assert links.urls[0] == PLAIN_URL


@pytest.mark.parametrize("status", [401, 403])
async def test_check_takes_a_401_or_403_for_a_private_bucket(status: int) -> None:
    assert await _check(FakeStorage(), FakeLinks(status, 200, 403)) is None


async def test_anonymous_read_of_uploads_that_succeeds_fails_the_private_step() -> None:
    storage = FakeStorage()

    assert await _check(storage, FakeLinks(200)) == StorageCheckFailure(
        step="private", reason="status_200", probe_left=False
    )
    assert storage.objects == {}


async def test_check_fails_the_private_step_when_nothing_answers() -> None:
    assert await _check(
        FakeStorage(), FakeLinks(ObjectStorageError("link", "unavailable"))
    ) == StorageCheckFailure(step="private", reason="unavailable", probe_left=False)


async def _media(storage: FakeStorage, links: FakeLinks) -> StorageCheckFailure | None:
    return await check_media_public(
        storage=storage, links=links, public_url=PUBLIC_URL, probe_key=MEDIA_KEY
    )


async def test_media_probe_is_put_read_anonymously_and_deleted() -> None:
    storage = FakeStorage()
    links = FakeLinks(200)

    assert await _media(storage, links) is None

    assert links.urls == [PUBLIC_URL]
    assert storage.calls == ["put", "delete", "get"]
    assert storage.objects == {}


async def test_media_check_fails_when_the_put_fails() -> None:
    storage = FakeStorage()
    storage.failing = {"put"}

    assert await _media(storage, FakeLinks()) == StorageCheckFailure(
        step="media_put", reason="denied", probe_left=False
    )


@pytest.mark.parametrize("status", [403, 404])
async def test_media_check_fails_when_the_public_url_is_not_readable(status: int) -> None:
    storage = FakeStorage()

    assert await _media(storage, FakeLinks(status)) == StorageCheckFailure(
        step="media_public", reason=f"status_{status}", probe_left=False
    )
    assert storage.objects == {}


async def test_media_check_fails_when_the_public_url_cannot_be_opened() -> None:
    assert await _media(
        FakeStorage(), FakeLinks(ObjectStorageError("link", "unavailable"))
    ) == StorageCheckFailure(step="media_public", reason="unavailable", probe_left=False)


async def test_media_check_fails_when_the_delete_fails_or_does_not_take() -> None:
    failing = FakeStorage()
    failing.failing = {"delete"}
    assert await _media(failing, FakeLinks(200)) == StorageCheckFailure(
        step="media_delete", reason="denied", probe_left=True
    )

    kept = FakeStorage()
    kept.keep_on_delete = True
    assert await _media(kept, FakeLinks(200)) == StorageCheckFailure(
        step="media_delete", reason="still_there", probe_left=True
    )


async def test_media_check_reports_a_probe_it_could_not_clean_up() -> None:
    storage = FakeStorage()
    storage.failing = {"delete"}

    assert await _media(storage, FakeLinks(403)) == StorageCheckFailure(
        step="media_public", reason="status_403", probe_left=True
    )
