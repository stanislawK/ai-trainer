"""Moving a stored picture out of the public media bucket and back (ADR-0031 `removed`, #556).

A removed picture's object goes to the private uploads bucket under `quarantine/<storage_key>`,
so its public URL stops serving it; Undo or Restore moves it back. Each move copies first and
deletes after, so a failure never loses the object: at worst it is in both buckets. Every move
can be retried, and finds the state a failed one left. Purging Cloudflare's cached copy is the
takedown's step after the move (#557).

`MediaTakedown` is the takedown path itself, shared by the admin's Remove (#556) and an athlete's
"Looks wrong" on a picture drawn on the card (#644): the link is hidden first, then a stored
picture's object moves to quarantine unless another live link shows the same asset, then its
public URL is purged."""

import logging
from dataclasses import dataclass

from ai_trainer.application.ports.cdn_cache import CdnCachePort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.media import ExerciseMediaReviewPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.cdn_cache import CdnCacheError
from ai_trainer.domain.media import ExerciseMediaReviewRow, MediaLinkStatus
from ai_trainer.domain.object_storage import (
    ObjectNotFoundError,
    ObjectStorageError,
    media_public_url,
)

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


@dataclass(frozen=True, slots=True)
class TakedownResult:
    # The status this call removed the link from; None when the link was not live (already
    # removed, or restored again since).
    removed_from: MediaLinkStatus | None
    # The link is live by now (restored in another tab): nothing was moved.
    live: MediaLinkStatus | None = None
    # The object could not leave the public bucket; another takedown retries the move.
    still_public: bool = False
    # Another link still shows the same asset, so its file was left public on purpose.
    shared: bool = False
    # The cache purge failed or was not tried, so a cached copy may still be served for a while.
    cache_stale: bool = False


class MediaTakedown:
    def __init__(
        self,
        *,
        media: ExerciseMediaReviewPort,
        quarantine: MediaQuarantine,
        clock: ClockPort,
        public_base_url: str | None,
        cdn: CdnCachePort | None = None,
    ) -> None:
        self._media = media
        self._quarantine = quarantine
        self._clock = clock
        self._base_url = public_base_url
        self._cdn = cdn

    async def take_down(self, row: ExerciseMediaReviewRow) -> TakedownResult:
        """Hide the link at once, then move a stored picture's object to quarantine. A link
        already removed keeps its first removal and has the move retried. A link restored in
        another tab since is left live with its file public, and says its status."""
        link_id = row.link_id
        removed_from = await self._media.remove(link_id, at=self._clock.now())
        if removed_from is None:
            # Nothing changed: read again, so a link that is live by now keeps its file.
            now = await self._media.review_link(link_id)
            if now is not None and now.status is not MediaLinkStatus.REMOVED:
                return TakedownResult(removed_from=None, live=now.status)
        key = row.asset.storage_key
        if key is None:
            return TakedownResult(removed_from=removed_from)
        # Read from the table, so the asset always has its ID.
        asset_id = row.asset.id
        if asset_id is not None and await self._media.has_live_link_besides(asset_id, link_id):
            return TakedownResult(removed_from=removed_from, shared=True)
        hidden = await self._quarantine.hide(key)
        stale = hidden and await self._purge(key)
        return TakedownResult(removed_from=removed_from, still_public=not hidden, cache_stale=stale)

    async def _purge(self, key: str) -> bool:
        """Purge the picture's public URL; True when a cached copy may still be served: the
        purge failed, or no Cloudflare settings are set so none was tried. False when there is
        no public URL (no `MEDIA_PUBLIC_BASE_URL`) or the purge succeeded."""
        url = media_public_url(self._base_url, key)
        if url is None:
            return False
        if self._cdn is None:
            logger.info("cache purge skipped: no Cloudflare purge settings")
            return True
        try:
            await self._cdn.purge(url)
        except CdnCacheError as error:
            logger.warning("cache purge failed: %s", error)
            return True
        return False
