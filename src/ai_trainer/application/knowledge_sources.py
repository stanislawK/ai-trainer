"""The admin's knowledge sources (ADR-0033, PRD B7): upload, delete and re-ingest.

An upload is checked whole before anything is stored. The file goes to the bucket first and the
source row second, so a row never names a missing object; when the row can't be written, the
object is removed again.
"""

import asyncio
import contextlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid4

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.knowledge import KnowledgeSourcesRepositoryPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.knowledge import (
    CONTENT_TYPES,
    REINGESTABLE,
    DuplicateSourceError,
    KnowledgeFileType,
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    NewKnowledgeSource,
    UploadAnswers,
    UploadField,
    UploadProblem,
    check_file,
    content_hash,
    read_details,
)
from ai_trainer.domain.object_storage import ObjectStorageError


class IncomingFile(Protocol):
    """An uploaded file as the route has it: Starlette's `UploadFile` fits."""

    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class SourceStored:
    source: KnowledgeSource


@dataclass(frozen=True)
class UploadRefused:
    problems: Mapping[UploadField, UploadProblem]


@dataclass(frozen=True)
class AlreadyStored:
    """The same file is already a source; nothing changed."""

    source: KnowledgeSource


@dataclass(frozen=True)
class StorageUnavailable:
    reason: Literal["not_configured", "failed"]


@dataclass(frozen=True)
class SourceDeleted:
    pass


@dataclass(frozen=True)
class SourceNotFound:
    pass


@dataclass(frozen=True)
class ReingestQueued:
    source: KnowledgeSource


@dataclass(frozen=True)
class NotReingestable:
    """Still `uploaded` or `ingesting`: the worker has it already."""

    source: KnowledgeSource


type UploadOutcome = SourceStored | UploadRefused | AlreadyStored | StorageUnavailable
type DeleteOutcome = SourceDeleted | SourceNotFound | StorageUnavailable
type ReingestOutcome = ReingestQueued | NotReingestable | SourceNotFound


class KnowledgeSources:
    def __init__(
        self,
        *,
        sources: KnowledgeSourcesRepositoryPort,
        storage: ObjectStoragePort | None,
        clock: ClockPort,
        max_upload_bytes: int,
        sports: Collection[str],
    ) -> None:
        self._sources = sources
        # None while the `OBJECT_STORAGE_*` settings are unset (ADR-0033): the list still shows.
        self._storage = storage
        self._clock = clock
        self._max_upload_bytes = max_upload_bytes
        self._sports = sports

    async def list(self) -> Sequence[KnowledgeSource]:
        return await self._sources.list_all()

    async def upload(
        self, admin_id: UUID, answers: UploadAnswers, file: IncomingFile | None
    ) -> UploadOutcome:
        # One byte past the limit is enough to tell a file is too large.
        data = await file.read(self._max_upload_bytes + 1) if file is not None else b""
        file_name = file.filename if file is not None else None
        # Up to KNOWLEDGE_MAX_UPLOAD_MB of decoding and hashing, kept off the event loop.
        file_type, digest = await asyncio.to_thread(self._inspect, file_name, data)
        if isinstance(file_type, KnowledgeFileType):
            existing = await self._sources.get_by_hash(digest)
            if existing is not None:
                return AlreadyStored(source=existing)
        details = read_details(answers, known_sports=self._sports)
        problems = dict(details) if isinstance(details, dict) else {}
        if isinstance(file_type, UploadProblem):
            problems = {UploadField.FILE: file_type, **problems}
        if problems:
            return UploadRefused(problems=problems)
        assert isinstance(file_type, KnowledgeFileType)
        assert not isinstance(details, dict)
        assert file_name is not None
        if self._storage is None:
            return StorageUnavailable(reason="not_configured")
        source_id = uuid4()
        source = NewKnowledgeSource(
            **details.model_dump(),
            id=source_id,
            # Until the form asks for a licence, its note is an admin's `other` (ADR-0036).
            license_id=LicenseId.OTHER,
            license_source=LicenseSource.ADMIN,
            content_hash=digest,
            file_type=file_type,
            file_name=file_name,
            size_bytes=len(data),
            storage_key=f"sources/{source_id}",
            uploaded_by_user_id=admin_id,
            uploaded_at=self._clock.now(),
        )
        return await self._store(self._storage, source, data)

    def _inspect(
        self, file_name: str | None, data: bytes
    ) -> tuple[KnowledgeFileType | UploadProblem, str]:
        return check_file(file_name, data, max_bytes=self._max_upload_bytes), content_hash(data)

    async def _store(
        self, storage: ObjectStoragePort, source: NewKnowledgeSource, data: bytes
    ) -> UploadOutcome:
        assert source.storage_key is not None
        assert source.file_type is not None
        try:
            await storage.put(
                source.storage_key, data, content_type=CONTENT_TYPES[source.file_type]
            )
        except ObjectStorageError:
            return StorageUnavailable(reason="failed")
        try:
            return SourceStored(source=await self._sources.add(source))
        except DuplicateSourceError:
            # The same file was stored by another upload since the lookup above.
            await _discard(storage, source.storage_key)
            existing = await self._sources.get_by_hash(source.content_hash)
            assert existing is not None
            return AlreadyStored(source=existing)
        except BaseException:
            await _discard(storage, source.storage_key)
            raise

    async def delete(self, source_id: UUID) -> DeleteOutcome:
        """Removes the file, then the source. A source being ingested may go too: the worker
        (#423) has to cope with that."""
        source = await self._sources.get(source_id)
        if source is None:
            return SourceNotFound()
        if source.storage_key is not None:
            if self._storage is None:
                return StorageUnavailable(reason="not_configured")
            try:
                await self._storage.delete(source.storage_key)
            except ObjectStorageError:
                return StorageUnavailable(reason="failed")
        return SourceDeleted() if await self._sources.delete(source_id) else SourceNotFound()

    async def reingest(self, admin_id: UUID, source_id: UUID) -> ReingestOutcome:
        source = await self._sources.get(source_id)
        if source is None:
            return SourceNotFound()
        if source.status not in REINGESTABLE:
            return NotReingestable(source=source)
        queued = await self._sources.queue_reingest(source_id, requested_by=admin_id)
        if queued is not None:
            return ReingestQueued(source=queued)
        # Its status changed since the lookup.
        current = await self._sources.get(source_id)
        return SourceNotFound() if current is None else NotReingestable(source=current)


async def _discard(storage: ObjectStoragePort, key: str) -> None:
    """Removes an object no source names. If that fails too, the orphan is harmless, and the
    outcome or error that led here is what the admin needs to see."""
    with contextlib.suppress(ObjectStorageError):
        await storage.delete(key)
