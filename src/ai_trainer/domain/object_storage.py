from typing import Literal

ObjectStorageOperation = Literal["put", "get", "delete", "presign", "link"]
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
