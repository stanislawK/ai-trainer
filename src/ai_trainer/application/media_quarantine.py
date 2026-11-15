"""Moving a stored picture out of the public media bucket and back (ADR-0031 `removed`, #556).

A removed picture's object goes to the private uploads bucket under `quarantine/<storage_key>`,
so its public URL stops serving it; Undo or Restore moves it back. Each move copies first and
deletes after, so a failure never loses the object: at worst it is in both buckets. Every move
can be retried, and finds the state a failed one left. Purging Cloudflare's cached copy is left
to a follow-up ticket (ADR-0031's #556 amendment)."""

import logging

from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError

logger = logging.getLogger(__name__)

QUARANTINE_PREFIX = "quarantine/"


def quarantine_key(storage_key: str) -> str:
    return f"{QUARANTINE_PREFIX}{storage_key}"


class MediaQuarantine:
    """Either bucket may be None while its setting is unset; then nothing is moved."""

    def __init__(self, *, media: ObjectStoragePort | None, uploads: ObjectStoragePort | None):
        self._media = media
        self._uploads = uploads

    async def hide(self, storage_key: str) -> bool:
        """Move the object to quarantine. True when it is no longer in the media bucket."""
        if self._media is None or self._uploads is None:
            return False
        try:
            try:
                stored = await self._media.read(storage_key)
            except ObjectNotFoundError:
                # Already moved, or never there: either way the public URL serves nothing.
                return True
            await self._uploads.put(
                quarantine_key(storage_key), stored.data, content_type=stored.content_type
            )
            await self._media.delete(storage_key)
        except ObjectStorageError as error:
            logger.warning("media quarantine failed: %s", error)
            return False
        return True

    async def release(self, storage_key: str) -> bool:
        """Move the object back to the media bucket. True when it is there afterwards."""
        if self._media is None or self._uploads is None:
            return False
        try:
            try:
                stored = await self._uploads.read(quarantine_key(storage_key))
            except ObjectNotFoundError:
                # A failed hide left it public: it is already where it belongs.
                await self._media.size(storage_key)
                return True
            await self._media.put(storage_key, stored.data, content_type=stored.content_type)
        except ObjectStorageError as error:
            logger.warning("media release failed: %s", error)
            return False
        try:
            await self._uploads.delete(quarantine_key(storage_key))
        except ObjectStorageError as error:
            # The object is public again; a stale private copy is harmless.
            logger.warning("media release left its quarantine copy: %s", error)
        return True
