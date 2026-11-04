from datetime import timedelta
from typing import Protocol


class ObjectStoragePort(Protocol):
    """One private bucket (ADR-0033). Every method raises `ObjectStorageError` on failure."""

    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes:
        """The object's bytes. Raises `ObjectNotFoundError` when it isn't there."""
        ...

    async def size(self, key: str) -> int:
        """The object's length in bytes, without reading them. Raises `ObjectNotFoundError` when
        it isn't there."""
        ...

    async def delete(self, key: str) -> None:
        """Removes the object. An object that isn't there is not a failure."""
        ...

    async def presigned_get(self, key: str, *, expires_in: timedelta) -> str:
        """A link that reads the object until `expires_in` has passed. It carries the key ID and
        a signature, so it is never logged."""
        ...

    async def presigned_put(self, key: str, *, expires_in: timedelta) -> str:
        """A link that writes the object until `expires_in` has passed (#460). It can't pin the
        content type or length, so whoever reads the object checks both. Never logged."""
        ...


class PresignedLinkPort(Protocol):
    """Opens a presigned link the way its holder would, for `ai-trainer storage-check`."""

    async def status(self, link: str) -> int:
        """The HTTP status the link answers with. Raises `ObjectStorageError` when nothing does."""
        ...
