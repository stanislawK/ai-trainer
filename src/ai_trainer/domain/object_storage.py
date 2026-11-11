from typing import Literal
from urllib.parse import quote

ObjectStorageOperation = Literal["put", "get", "head", "delete", "presign", "link"]
ObjectStorageFailureReason = Literal[
    "not_found", "denied", "unavailable", "invalid_key", "invalid_content_type"
]


class ObjectStorageError(Exception):
    """An object storage call failed (ADR-0033). It names the operation and a closed reason only:
    a service's own message can quote the access key, and a link carries a signature."""

    def __init__(
        self, operation: ObjectStorageOperation, reason: ObjectStorageFailureReason
    ) -> None:
        super().__init__(f"object storage {operation} failed: {reason}")
        self.operation: ObjectStorageOperation = operation
        self.reason: ObjectStorageFailureReason = reason


class ObjectNotFoundError(ObjectStorageError):
    def __init__(self, operation: ObjectStorageOperation) -> None:
        super().__init__(operation, "not_found")


def media_public_url(base_url: str | None, storage_key: str) -> str | None:
    """A stored picture's public URL: the media base plus its key (ADR-0031, ADR-0033). R2's
    custom domain can't be a presigned link's host, so the URL is built, never signed. None while
    the base is unset. A trailing slash on the base makes no difference."""
    if base_url is None:
        return None
    return f"{base_url.rstrip('/')}/{quote(storage_key, safe='/')}"
