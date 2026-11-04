"""Staging files for upload by presigned PUT (ADR-0036 "By file", PRD B7, #460).

A token client stages a file's metadata and gets a link that writes it straight to `uploads`, so
the bytes never cross the app, Cloudflare's 100 MB cap or a model (invariant 3). It then
registers the upload, which runs ADR-0033's checks on the stored object and leaves the source
`proposed` (invariant 1).

Nothing is written to the database before register. The validated metadata waits in the bucket
as `staging/<id>.json`, so only an upload a stage issued can register. The file's key is
`sources/<id>`, and the source takes the upload's id. Only an object no source names is ever
deleted.
"""

import asyncio
import contextlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from ai_trainer.application.knowledge_sources import StorageUnavailable
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.knowledge import KnowledgeSourcesRepositoryPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.knowledge import (
    KnowledgeFileType,
    KnowledgeSource,
    NewKnowledgeSource,
    SourceStatus,
    UploadProblem,
    check_file,
    content_hash,
    file_type_for,
)
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError
from ai_trainer.domain.source_template import SourceTemplate

# Long enough for a large book over a slow uplink, well inside R2's 1 second to 7 days.
LINK_LIFETIME = timedelta(hours=1)
_STAGED_CONTENT_TYPE = "application/json"


def upload_key(upload_id: UUID) -> str:
    """Where the client's PUT writes the file; the source's `storage_key` once registered."""
    return f"sources/{upload_id}"


def staged_key(upload_id: UUID) -> str:
    """Where the stage keeps the validated metadata until register."""
    return f"staging/{upload_id}.json"


class _Staged(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file_name: str
    # #456's template, as validated at the stage, under its CSL-JSON keys.
    source: dict[str, JsonValue]


@dataclass(frozen=True)
class UploadReady:
    upload_id: UUID
    # A secret: it carries the key ID and a signature, so it is never logged (ADR-0033).
    put_url: str
    expires_at: datetime


@dataclass(frozen=True)
class StageCovered:
    """A source with the template's PMC ID, DOI, PMID or ISBN exists already."""

    source: KnowledgeSource


@dataclass(frozen=True)
class StageRefused:
    # A field's path, e.g. `licence.attribution` or `file_name`, to its error type.
    problems: Mapping[str, str]


@dataclass(frozen=True)
class Registered:
    source: KnowledgeSource


@dataclass(frozen=True)
class RegisterCovered:
    """The file, or a source with its identifiers, is stored already; the new object is gone."""

    source: KnowledgeSource


@dataclass(frozen=True)
class RegisterRefused:
    """The object is empty, too large or of the wrong type; it is deleted with its stage."""

    problem: UploadProblem


@dataclass(frozen=True)
class ObjectMissing:
    """Nothing was PUT yet. The stage stands, so a PUT and a second register can follow."""


@dataclass(frozen=True)
class UploadNotFound:
    """No stage issued this upload, or its stage was used up."""


type StageOutcome = UploadReady | StageCovered | StageRefused | StorageUnavailable
type RegisterOutcome = (
    Registered
    | RegisterCovered
    | RegisterRefused
    | ObjectMissing
    | UploadNotFound
    | StorageUnavailable
)


class FileStaging:
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
        # None while the `OBJECT_STORAGE_*` settings are unset (ADR-0033).
        self._storage = storage
        self._clock = clock
        # The form's limit (KNOWLEDGE_MAX_UPLOAD_MB), owner-chosen at #460's plan gate.
        self._max_upload_bytes = max_upload_bytes
        self._sports = sports

    async def stage(self, file_name: str, source: Mapping[str, JsonValue]) -> StageOutcome:
        if self._storage is None:
            return StorageUnavailable(reason="not_configured")
        template = self._template(file_name, source)
        if isinstance(template, StageRefused):
            return template
        covered = await self._covering(template, digest=None)
        if covered is not None:
            return StageCovered(source=covered)
        upload_id = uuid4()
        staged = _Staged(
            file_name=file_name, source=template.model_dump(mode="json", by_alias=True)
        )
        try:
            await self._storage.put(
                staged_key(upload_id),
                staged.model_dump_json().encode(),
                content_type=_STAGED_CONTENT_TYPE,
            )
            put_url = await self._storage.presigned_put(
                upload_key(upload_id), expires_in=LINK_LIFETIME
            )
        except ObjectStorageError:
            # A metadata object left behind names no source and is harmless.
            return StorageUnavailable(reason="failed")
        return UploadReady(
            upload_id=upload_id, put_url=put_url, expires_at=self._clock.now() + LINK_LIFETIME
        )

    def _template(
        self, file_name: str, source: Mapping[str, JsonValue]
    ) -> SourceTemplate | StageRefused:
        problems: dict[str, str] = {}
        if file_type_for(file_name) is None:
            problems["file_name"] = UploadProblem.UNLISTED_TYPE.value
        try:
            template = SourceTemplate.model_validate(source, context={"sports": self._sports})
        except ValidationError as exc:
            for error in exc.errors():
                problems[".".join(str(part) for part in error["loc"])] = error["type"]
            return StageRefused(problems=problems)
        return StageRefused(problems=problems) if problems else template

    async def register(self, admin_id: UUID, upload_id: UUID) -> RegisterOutcome:
        storage = self._storage
        if storage is None:
            return StorageUnavailable(reason="not_configured")
        # A retry after a lost answer: the stage is used up, and the source answers for it.
        stored = await self._sources.get(upload_id)
        if stored is not None:
            return Registered(source=stored)
        fetched = await self._fetch(storage, upload_id)
        if not isinstance(fetched, tuple):
            return fetched
        staged, data = fetched
        # Up to KNOWLEDGE_MAX_UPLOAD_MB of decoding and hashing, kept off the event loop.
        file_type, digest = await asyncio.to_thread(self._inspect, staged.file_name, data)
        if isinstance(file_type, UploadProblem):
            await _discard(storage, upload_id)
            return RegisterRefused(problem=file_type)
        # Validated at the stage; a sport removed from the registry since is a bug to see.
        template = SourceTemplate.model_validate(staged.source, context={"sports": self._sports})
        covered = await self._covering(template, digest=digest)
        if covered is not None:
            await _discard(storage, upload_id)
            return RegisterCovered(source=covered)
        source = self._proposal(
            admin_id,
            upload_id,
            template,
            _StoredFile(staged.file_name, file_type, digest, len(data)),
        )
        return await self._add(storage, source, template)

    async def _fetch(
        self, storage: ObjectStoragePort, upload_id: UUID
    ) -> tuple[_Staged, bytes] | RegisterOutcome:
        """The stage's metadata and the object's bytes; the object's size is read first, so an
        oversized one is refused without reading it."""
        try:
            staged = _Staged.model_validate_json(await storage.get(staged_key(upload_id)))
        except ObjectNotFoundError:
            return UploadNotFound()
        except ObjectStorageError:
            return StorageUnavailable(reason="failed")
        try:
            size = await storage.size(upload_key(upload_id))
            if size > self._max_upload_bytes:
                await _discard(storage, upload_id)
                return RegisterRefused(problem=UploadProblem.TOO_LARGE)
            return staged, await storage.get(upload_key(upload_id))
        except ObjectNotFoundError:
            return ObjectMissing()
        except ObjectStorageError:
            return StorageUnavailable(reason="failed")

    def _inspect(
        self, file_name: str, data: bytes
    ) -> tuple[KnowledgeFileType | UploadProblem, str]:
        return check_file(file_name, data, max_bytes=self._max_upload_bytes), content_hash(data)

    async def _covering(
        self, template: SourceTemplate, *, digest: str | None
    ) -> KnowledgeSource | None:
        """A stored source with the file's hash or one of the template's identifiers."""
        if digest is not None:
            same_file = await self._sources.get_by_hash(digest)
            if same_file is not None:
                return same_file
        return await self._sources.find_by_identifiers(
            pmcid=template.pmcid, doi=template.doi, pmid=template.pmid, isbn=template.isbn
        )

    async def _add(
        self, storage: ObjectStoragePort, source: NewKnowledgeSource, template: SourceTemplate
    ) -> RegisterOutcome:
        added = await self._sources.add_if_new(source)
        if added is not None:
            with contextlib.suppress(ObjectStorageError):
                await storage.delete(staged_key(source.id))
            return Registered(source=added)
        # Another register of this upload stored it since the lookup: its row names the object.
        winner = await self._sources.get(source.id)
        if winner is not None:
            return Registered(source=winner)
        covered = await self._covering(template, digest=source.content_hash)
        if covered is not None:
            await _discard(storage, source.id)
            return RegisterCovered(source=covered)
        # The source it clashed with was deleted since; the stage stands, so a retry registers.
        return StorageUnavailable(reason="failed")

    def _proposal(
        self, admin_id: UUID, upload_id: UUID, template: SourceTemplate, file: _StoredFile
    ) -> NewKnowledgeSource:
        licence = template.licence
        return NewKnowledgeSource(
            **template.details().model_dump(),
            # Without a licence the admin sets one before approving (#461).
            license_id=None if licence is None else licence.license_id,
            license_source=None if licence is None else licence.license_source,
            license_url=None if licence is None else licence.license_url,
            copyright_statement=None if licence is None else licence.copyright_statement,
            attribution=None if licence is None else licence.attribution,
            id=upload_id,
            status=SourceStatus.PROPOSED,
            content_hash=file.digest,
            file_type=file.file_type,
            file_name=file.name,
            size_bytes=file.size,
            storage_key=upload_key(upload_id),
            uploaded_by_user_id=admin_id,
            uploaded_at=self._clock.now(),
        )


@dataclass(frozen=True)
class _StoredFile:
    name: str
    file_type: KnowledgeFileType
    digest: str
    size: int


async def _discard(storage: ObjectStoragePort, upload_id: UUID) -> None:
    """Removes an upload no source names, with its stage. If that fails, the orphan is harmless,
    and the refusal that led here is what the client needs to see."""
    for key in (upload_key(upload_id), staged_key(upload_id)):
        with contextlib.suppress(ObjectStorageError):
            await storage.delete(key)
