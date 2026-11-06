"""Gives an approved PMC source its file (ADR-0036, #463).

Staging (#458) stores a PMC article as a source with no bytes: no `storage_key`, and the hash of
`pmc:<PMCID>` standing in for the file's. The first time the worker claims it, this fetches the
article's JATS XML by its PMC ID, stores it in `uploads` and records its key, name, size and
hash on the source, under the worker's claim. The worker then reads the bytes it holds like any
uploaded file's.

- The key is `sources/<source id>`, so a worker that crashed between the upload and the record
  is followed by one that overwrites the same object.
- A hash that is another source's means the article is a duplicate of that source (the #458
  amendment): the object just stored is removed and the source fails `duplicate_source`. It is
  never silently merged.
- A claim lost before the record removes the object too, then stops the work.
- A source that has its file already (an ingested article being re-ingested) is not fetched
  again.

Only the PMC ID leaves the app, and the file never passes through a model (invariants 3 and 7).
"""

import contextlib
from uuid import UUID

from ai_trainer.application.ports.knowledge import KnowledgeIngestionRepositoryPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.application.ports.pmc import PmcArticleFilesPort
from ai_trainer.domain.knowledge import (
    CONTENT_TYPES,
    KnowledgeFileType,
    KnowledgeSource,
    content_hash,
)
from ai_trainer.domain.knowledge_ingestion import ClaimLostError, FileRecord, IngestFailure
from ai_trainer.domain.object_storage import ObjectStorageError
from ai_trainer.domain.pmc import PmcNotFound


def needs_fetch(source: KnowledgeSource) -> bool:
    """A PMC article staged by ID, whose JATS has not been fetched yet."""
    return (
        source.storage_key is None
        and source.file_type is KnowledgeFileType.JATS
        and source.pmcid is not None
    )


class PmcSourceFile:
    def __init__(
        self,
        *,
        files: PmcArticleFilesPort,
        storage: ObjectStoragePort,
        repository: KnowledgeIngestionRepositoryPort,
    ) -> None:
        self._files = files
        self._storage = storage
        self._repository = repository

    async def fetch(self, source: KnowledgeSource, claim_id: UUID) -> bytes | IngestFailure:
        """The source's JATS, now stored and recorded; or why it could not be. Raises
        `ClaimLostError` when the source was deleted or taken over meanwhile."""
        pmcid = source.pmcid
        assert pmcid is not None, "needs_fetch() was checked"
        fetched = await self._files.fetch_jats(pmcid)
        if isinstance(fetched, PmcNotFound):
            return IngestFailure.PMC_NOT_FOUND
        if not isinstance(fetched, bytes):
            return IngestFailure.PMC_FETCH_FAILED
        key = f"sources/{source.id}"
        try:
            await self._storage.put(
                key, fetched, content_type=CONTENT_TYPES[KnowledgeFileType.JATS]
            )
        except ObjectStorageError:
            return IngestFailure.STORAGE_FAILED
        recorded = await self._repository.record_file(
            source.id,
            claim_id=claim_id,
            storage_key=key,
            content_hash=content_hash(fetched),
            size_bytes=len(fetched),
            file_name=f"{pmcid}.xml",
        )
        if recorded is FileRecord.RECORDED:
            return fetched
        # Nothing refers to the object: it is not the source's file.
        with contextlib.suppress(ObjectStorageError):
            await self._storage.delete(key)
        if recorded is FileRecord.DUPLICATE:
            return IngestFailure.DUPLICATE_SOURCE
        raise ClaimLostError
