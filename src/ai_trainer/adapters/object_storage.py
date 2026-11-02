"""Object storage on any S3-compatible service: R2 in production, versitygw locally (ADR-0033)."""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import urlopen

import obstore
from obstore.exceptions import (
    BaseError,
    InvalidPathError,
    PermissionDeniedError,
    UnauthenticatedError,
)
from obstore.store import S3Store

from ai_trainer.domain.object_storage import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageOperation,
)

if TYPE_CHECKING:
    # Only in obstore's stubs: not importable at runtime, and no framework reads this annotation.
    from obstore.store import S3Credential

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = timedelta(seconds=5)
# obstore's defaults are 10 retries over 3 minutes, which an admin's upload would sit through.
_MAX_RETRIES = 2
# R2's and SigV4's own bounds on a presigned link (ADR-0033).
_SHORTEST_LINK = timedelta(seconds=1)
_LONGEST_LINK = timedelta(days=7)
# What obstore raises, per its stubs: `BaseError`, and the built-in `ValueError` or
# `FileNotFoundError`. A dropped connection can surface as another `OSError`.
_LIBRARY_ERRORS = (BaseError, OSError, ValueError)


def _domain_error(operation: ObjectStorageOperation, exc: Exception) -> ObjectStorageError:
    """Drops the library's message: a service's refusal can quote the access key, and obstore's
    error text includes the request's URL."""
    match exc:
        case FileNotFoundError():
            return ObjectNotFoundError(operation)
        case PermissionDeniedError() | UnauthenticatedError():
            error = ObjectStorageError(operation, "denied")
        case InvalidPathError() | ValueError() | TypeError():
            error = ObjectStorageError(operation, "invalid_key")
        case _:
            error = ObjectStorageError(operation, "unavailable")
    logger.warning("%s (%s)", error, type(exc).__name__)
    return error


class ObstoreObjectStorage:
    """Implements `ObjectStoragePort` on one bucket."""

    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        timeout_seconds: float,
    ) -> None:
        timeout = timedelta(seconds=timeout_seconds)

        def credentials() -> S3Credential:
            return {
                "access_key_id": access_key_id,
                "secret_access_key": secret_access_key,
                "token": None,
                "expires_at": None,
            }

        self._store = S3Store(
            bucket,
            endpoint=endpoint,
            region=region,
            # Not as keys: a stray AWS_SESSION_TOKEN would then be signed into every request and
            # link. obstore reads its other settings from AWS_* too; those passed here win.
            credential_provider=credentials,
            virtual_hosted_style_request=False,
            client_options={
                "allow_http": endpoint.startswith("http://"),
                "connect_timeout": _CONNECT_TIMEOUT,
                "timeout": timeout,
            },
            retry_config={"max_retries": _MAX_RETRIES, "retry_timeout": timeout},
        )

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        try:
            await self._store.put_async(key, data, attributes={"Content-Type": content_type})
        except _LIBRARY_ERRORS as exc:
            raise _domain_error("put", exc) from None

    async def get(self, key: str) -> bytes:
        try:
            result = await self._store.get_async(key)
            return bytes(await result.bytes_async())
        except _LIBRARY_ERRORS as exc:
            raise _domain_error("get", exc) from None

    async def delete(self, key: str) -> None:
        try:
            await self._store.delete_async(key)
        except _LIBRARY_ERRORS as exc:
            raise _domain_error("delete", exc) from None

    async def presigned_get(self, key: str, *, expires_in: timedelta) -> str:
        if not _SHORTEST_LINK <= expires_in <= _LONGEST_LINK:
            raise ValueError("a presigned link's time must be between 1 second and 7 days")
        try:
            return await obstore.sign_async(self._store, "GET", key, expires_in)
        except (*_LIBRARY_ERRORS, TypeError) as exc:
            # Signing reports an invalid path as a `TypeError`, unlike the store's own methods.
            raise _domain_error("presign", exc) from None


def _open(link: str, timeout_seconds: float) -> int:
    try:
        with urlopen(link, timeout=timeout_seconds) as response:
            return int(response.status)
    except HTTPError as exc:
        exc.close()
        return exc.code


class UrllibPresignedLinks:
    """Implements `PresignedLinkPort`. On `urllib`, not `httpx2`, which logs every request's URL at
    INFO, and a presigned link's URL carries the key ID and a signature."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    async def status(self, link: str) -> int:
        try:
            return await asyncio.to_thread(_open, link, self._timeout_seconds)
        except OSError:
            raise ObjectStorageError("link", "unavailable") from None
