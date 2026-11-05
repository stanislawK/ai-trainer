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
    NewKnowledgeSource,
    ProposalDecision,
    SkipReason,
    SourceStatus,
    UploadProblem,
    check_file,
    content_hash,
)
from ai_trainer.domain.knowledge_form import (
    UploadAnswers,
    UploadField,
    read_details,
    read_licence,
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


@dataclass(frozen=True)
class SourcesPage:
    sources: Sequence[KnowledgeSource]
    # The cursor for the page after this one; None on the last page.
    next: UUID | None


@dataclass(frozen=True)
class Skipped:
    """A selected proposal left as it was, and why. `source` is None once it was deleted."""

    reason: SkipReason
    source: KnowledgeSource | None


@dataclass(frozen=True)
class ProposalsDecided:
    decision: ProposalDecision
    done: int
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True)
class LicenceSet:
    source: KnowledgeSource


@dataclass(frozen=True)
class LicenceRefused:
    problems: Mapping[UploadField, UploadProblem]


@dataclass(frozen=True)
class NotProposed:
    """Decided in another tab: its licence is no longer the admin's to set here."""

    source: KnowledgeSource


type UploadOutcome = SourceStored | UploadRefused | AlreadyStored | StorageUnavailable
type DeleteOutcome = SourceDeleted | SourceNotFound | StorageUnavailable
type ReingestOutcome = ReingestQueued | NotReingestable | SourceNotFound


@dataclass(frozen=True)
class AlreadyLicensed:
    """Set licence is for a proposal that has none; one read from PMC or the file keeps it."""

    source: KnowledgeSource


type LicenceOutcome = LicenceSet | LicenceRefused | NotProposed | AlreadyLicensed | SourceNotFound


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

    async def list_page(self, *, after: UUID | None, limit: int) -> SourcesPage:
        """One page of the manifest by `id` (#457). One row past `limit` tells whether another
        page follows, so the last page never names a cursor that leads to nothing."""
        rows = await self._sources.list_page(after=after, limit=limit + 1)
        page = rows[:limit]
        return SourcesPage(sources=page, next=page[-1].id if len(rows) > limit else None)

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
        # With no licence picked, the `ingest` container reads it from the file first, and
        # nothing is ingested until an admin approves it (the #454 amendment).
        licence = details.licence
        source = NewKnowledgeSource(
            **details.details().model_dump(),
            license_id=None if licence is None else licence.license_id,
            license_source=None if licence is None else licence.license_source,
            license_url=None if licence is None else licence.license_url,
            copyright_statement=None if licence is None else licence.copyright_statement,
            attribution=None if licence is None else licence.attribution,
            id=source_id,
            status=SourceStatus.DETECTING if licence is None else SourceStatus.UPLOADED,
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

    async def decide(
        self, admin_id: UUID, source_ids: Sequence[UUID], decision: ProposalDecision
    ) -> ProposalsDecided:
        """Approves or rejects the selected proposals in one statement (ADR-0036, #461). Each one
        the statement left alone is reported: decided in another tab, still without a licence,
        or deleted. The phone card and desktop row each post a source's ID, so it counts once."""
        wanted = list(dict.fromkeys(source_ids))
        changed = set(
            await self._sources.decide_proposals(
                wanted, status=decision.status, decided_by=admin_id
            )
        )
        left = [source_id for source_id in wanted if source_id not in changed]
        current = {source.id: source for source in await self._sources.get_many(left)}
        return ProposalsDecided(
            decision=decision,
            done=len(changed),
            skipped=tuple(_skipped(current.get(source_id)) for source_id in left),
        )

    async def set_licence(self, source_id: UUID, answers: UploadAnswers) -> LicenceOutcome:
        """An admin's licence on a proposal that has none, so it can be approved (the #454
        amendment)."""
        refused = _unlicensable(await self._sources.get(source_id))
        if refused is not None:
            return refused
        terms = read_licence(answers)
        if isinstance(terms, dict):
            return LicenceRefused(problems=terms)
        updated = await self._sources.set_licence(source_id, terms)
        if updated is not None:
            return LicenceSet(source=updated)
        # Decided, licensed or deleted in another tab since the lookup.
        current = await self._sources.get(source_id)
        return _unlicensable(current) or SourceNotFound()


def _unlicensable(
    source: KnowledgeSource | None,
) -> NotProposed | AlreadyLicensed | SourceNotFound | None:
    """Why Set licence can't apply to this source, or None when it can."""
    if source is None:
        return SourceNotFound()
    if source.status is not SourceStatus.PROPOSED:
        return NotProposed(source=source)
    if source.license_id is not None:
        return AlreadyLicensed(source=source)
    return None


def _skipped(source: KnowledgeSource | None) -> Skipped:
    if source is None:
        return Skipped(reason=SkipReason.GONE, source=None)
    if source.status is SourceStatus.PROPOSED:
        return Skipped(reason=SkipReason.UNLICENSED, source=source)
    return Skipped(reason=SkipReason.MOVED, source=source)


async def _discard(storage: ObjectStoragePort, key: str) -> None:
    """Removes an object no source names. If that fails too, the orphan is harmless, and the
    outcome or error that led here is what the admin needs to see."""
    with contextlib.suppress(ObjectStorageError):
        await storage.delete(key)
