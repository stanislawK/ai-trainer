"""The object storage round trip behind `ai-trainer storage-check` (ADR-0033, ticket #420)."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ai_trainer.application.ports.object_storage import ObjectStoragePort, PresignedLinkPort
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError

StorageCheckStep = Literal["put", "read", "link", "expiry", "delete"]

STEPS: tuple[StorageCheckStep, ...] = ("put", "read", "link", "expiry", "delete")
# The shortest link that a slow first request can still open.
LINK_SECONDS = 2
# Past the link's time by more than the clock skew between this machine and the service.
WAIT_SECONDS = LINK_SECONDS + 3

_PROBE = b"ai-trainer storage-check\n"
# How S3 services refuse an expired link. Only these prove it: a 429 or a 404 says something else
# went wrong, and the same link was served a moment before.
_EXPIRED_STATUSES = frozenset({400, 403})


class StorageCheckFailure(BaseModel):
    """The first step that failed, and whether the probe object is still in the bucket."""

    model_config = ConfigDict(frozen=True)

    step: StorageCheckStep
    reason: str
    probe_left: bool


class _StepFailedError(Exception):
    def __init__(self, step: StorageCheckStep, reason: str) -> None:
        super().__init__(step, reason)
        self.step: StorageCheckStep = step
        self.reason = reason


@contextmanager
def _step(step: StorageCheckStep) -> Iterator[None]:
    try:
        yield
    except ObjectStorageError as exc:
        raise _StepFailedError(step, exc.reason) from None


async def _read_back(storage: ObjectStoragePort, key: str) -> None:
    with _step("read"):
        content = await storage.get(key)
    if content != _PROBE:
        raise _StepFailedError("read", "mismatch")


async def _link_runs_out(
    storage: ObjectStoragePort,
    links: PresignedLinkPort,
    sleep: Callable[[float], Awaitable[None]],
    key: str,
) -> None:
    with _step("link"):
        link = await storage.presigned_get(key, expires_in=timedelta(seconds=LINK_SECONDS))
        fresh = await links.status(link)
    if fresh != 200:
        raise _StepFailedError("link", f"status_{fresh}")
    await sleep(WAIT_SECONDS)
    with _step("expiry"):
        expired = await links.status(link)
    if expired not in _EXPIRED_STATUSES:
        raise _StepFailedError("expiry", f"status_{expired}")


async def _delete_for_good(storage: ObjectStoragePort, key: str) -> None:
    with _step("delete"):
        await storage.delete(key)
        try:
            await storage.get(key)
        except ObjectNotFoundError:
            return
    raise _StepFailedError("delete", "still_there")


async def _removed(storage: ObjectStoragePort, key: str) -> bool:
    try:
        await storage.delete(key)
    except ObjectStorageError:
        return False
    return True


async def check_storage(
    *,
    storage: ObjectStoragePort,
    links: PresignedLinkPort,
    sleep: Callable[[float], Awaitable[None]],
    probe_key: str,
) -> StorageCheckFailure | None:
    """Puts a probe object, reads it back, opens a short link until it runs out, then deletes the
    object. Returns the first failure, or None. A probe left by a failed step is deleted too."""
    try:
        with _step("put"):
            await storage.put(probe_key, _PROBE, content_type="text/plain")
    except _StepFailedError as failed:
        return StorageCheckFailure(step=failed.step, reason=failed.reason, probe_left=False)
    try:
        await _read_back(storage, probe_key)
        await _link_runs_out(storage, links, sleep, probe_key)
    except _StepFailedError as failed:
        probe_left = not await _removed(storage, probe_key)
        return StorageCheckFailure(step=failed.step, reason=failed.reason, probe_left=probe_left)
    try:
        await _delete_for_good(storage, probe_key)
    except _StepFailedError as failed:
        return StorageCheckFailure(step=failed.step, reason=failed.reason, probe_left=True)
    return None
