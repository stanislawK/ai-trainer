"""The storage round trip with fakes (ADR-0033, ticket #420)."""

from datetime import timedelta

import pytest

from ai_trainer.application.storage_check import (
    LINK_SECONDS,
    WAIT_SECONDS,
    StorageCheckFailure,
    check_storage,
)
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError

KEY = "storage-check/probe"


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

    async def status(self, link: str) -> int:
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
        storage=storage, links=links, sleep=sleep or FakeSleep(), probe_key=KEY
    )


async def test_check_puts_reads_back_and_deletes() -> None:
    storage = FakeStorage()

    assert await _check(storage, FakeLinks(200, 403)) is None

    assert storage.calls == ["put", "get", "presigned_get", "delete", "get"]
    assert storage.objects == {}


async def test_check_waits_past_the_link_time_before_trying_it_again() -> None:
    storage = FakeStorage()
    sleep = FakeSleep()

    await _check(storage, FakeLinks(200, 403), sleep)

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

    assert await _check(storage, FakeLinks()) == StorageCheckFailure(
        step="read", reason="mismatch", probe_left=False
    )
    assert storage.objects == {}


async def test_check_fails_when_a_fresh_link_is_refused() -> None:
    storage = FakeStorage()

    assert await _check(storage, FakeLinks(403)) == StorageCheckFailure(
        step="link", reason="status_403", probe_left=False
    )
    assert storage.objects == {}


async def test_check_fails_when_a_link_cannot_be_opened() -> None:
    storage = FakeStorage()

    assert await _check(
        storage, FakeLinks(ObjectStorageError("link", "unavailable"))
    ) == StorageCheckFailure(step="link", reason="unavailable", probe_left=False)


async def test_check_fails_when_a_link_outlives_its_time() -> None:
    storage = FakeStorage()

    assert await _check(storage, FakeLinks(200, 200)) == StorageCheckFailure(
        step="expiry", reason="status_200", probe_left=False
    )
    assert storage.objects == {}


@pytest.mark.parametrize("status", [404, 429, 500])
async def test_check_takes_only_a_refusal_for_an_expired_link(status: int) -> None:
    assert await _check(FakeStorage(), FakeLinks(200, status)) == StorageCheckFailure(
        step="expiry", reason=f"status_{status}", probe_left=False
    )


@pytest.mark.parametrize("status", [400, 403])
async def test_check_accepts_the_statuses_services_refuse_an_expired_link_with(
    status: int,
) -> None:
    assert await _check(FakeStorage(), FakeLinks(200, status)) is None


async def test_check_fails_when_the_delete_fails() -> None:
    storage = FakeStorage()
    storage.failing = {"delete"}

    assert await _check(storage, FakeLinks(200, 403)) == StorageCheckFailure(
        step="delete", reason="denied", probe_left=True
    )


async def test_check_fails_when_the_object_survives_the_delete() -> None:
    storage = FakeStorage()
    storage.keep_on_delete = True

    assert await _check(storage, FakeLinks(200, 403)) == StorageCheckFailure(
        step="delete", reason="still_there", probe_left=True
    )


async def test_check_reports_a_probe_it_could_not_clean_up() -> None:
    storage = FakeStorage()
    storage.corrupt = True
    storage.failing = {"delete"}

    assert await _check(storage, FakeLinks()) == StorageCheckFailure(
        step="read", reason="mismatch", probe_left=True
    )
