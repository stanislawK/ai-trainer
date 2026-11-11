"""Fakes for storing a generated picture (ADR-0031, #535): an in-memory media bucket, and a clock
that never reads the wall clock."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError

FROZEN = datetime(2026, 11, 11, 23, 59, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return FROZEN


@dataclass
class InMemoryBucket:
    """Implements `ObjectStoragePort`; `fail_put` makes every put raise, as an unreachable
    bucket would."""

    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    fail_put: bool = False

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        if self.fail_put:
            raise ObjectStorageError("put", "unavailable")
        self.objects[key] = data
        self.content_types[key] = content_type

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError("get")
        return self.objects[key]

    async def size(self, key: str) -> int:
        return len(await self.get(key))

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def presigned_get(self, key: str, *, expires_in: timedelta) -> str:
        raise NotImplementedError

    async def presigned_put(self, key: str, *, expires_in: timedelta) -> str:
        raise NotImplementedError
