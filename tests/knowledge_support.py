"""Fakes and sample files for the knowledge upload tests (ADR-0033, #422).

Every sample carries a marker, so two tests never upload the same bytes: `content_hash` is
unique, and a second insert of a hash another test's open transaction holds would wait for it.
"""

import io
import zipfile
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TypedDict
from uuid import UUID

from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.domain.knowledge import (
    DuplicateSourceError,
    KnowledgeSource,
    NewKnowledgeSource,
    SourceStatus,
)
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry


def pdf_bytes(marker: str) -> bytes:
    return b"%PDF-1.7\n% " + marker.encode() + b"\n%%EOF\n"


def epub_bytes(marker: str) -> bytes:
    # EPUB OCF: `mimetype` first, stored uncompressed, with no extra field.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        archive.writestr("OEBPS/chapter.xhtml", f"<html><body>{marker}</body></html>")
    return buffer.getvalue()


def answers(**overrides: str | list[str]) -> dict[str, str | list[str]]:
    """A complete, valid set of the form's fields, as the browser posts them."""
    fields: dict[str, str | list[str]] = {
        "title": "Deload timing in resistance training",
        "author": "M. Kowalska et al.",
        "source_type": "research_paper",
        "evidence_tier": "peer_reviewed",
        "language": "en",
        "topics": "Deload, recovery",
        "license_id": "cc-by",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "copyright_statement": "© 2024 The Authors",
        "attribution": "M. Kowalska et al., Deload timing in resistance training, CC BY 4.0",
        "source_url": "https://example.org/deload",
        "sports": ["gym"],
    }
    fields.update(overrides)
    return fields


class MemoryStorage:
    """Implements `ObjectStoragePort` in memory; `failing` names operations that raise."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.failing: set[str] = set()

    def _check(self, operation: str) -> None:
        if operation in self.failing:
            raise ObjectStorageError("put" if operation == "put" else "delete", "unavailable")

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self._check("put")
        self.objects[key] = data
        self.content_types[key] = content_type

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError("get")
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self._check("delete")
        self.objects.pop(key, None)

    async def presigned_get(self, key: str, *, expires_in: timedelta) -> str:
        raise AssertionError("the upload page makes no links")


class MemorySources:
    """Implements `KnowledgeSourcesRepositoryPort` in memory."""

    def __init__(self) -> None:
        self.rows: dict[UUID, KnowledgeSource] = {}
        self.racing: KnowledgeSource | None = None

    async def list_all(self) -> Sequence[KnowledgeSource]:
        return sorted(self.rows.values(), key=lambda s: s.uploaded_at, reverse=True)

    async def list_page(self, *, after: UUID | None, limit: int) -> Sequence[KnowledgeSource]:
        # By id, as PostgreSQL orders `uuid`: byte order, the same as the text form's.
        ordered = sorted(self.rows.values(), key=lambda s: str(s.id))
        return [s for s in ordered if after is None or str(s.id) > str(after)][:limit]

    async def get(self, source_id: UUID) -> KnowledgeSource | None:
        return self.rows.get(source_id)

    async def get_by_hash(self, content_hash: str) -> KnowledgeSource | None:
        return next((s for s in self.rows.values() if s.content_hash == content_hash), None)

    async def add(self, source: NewKnowledgeSource) -> KnowledgeSource:
        if self.racing is not None:
            # Another upload of the same file committed between the lookup and this insert.
            self.rows[self.racing.id] = self.racing
            raise DuplicateSourceError
        if await self.get_by_hash(source.content_hash) is not None:
            raise DuplicateSourceError
        stored = KnowledgeSource(**source.model_dump())
        self.rows[stored.id] = stored
        return stored

    async def find_by_identifiers(
        self, *, pmcid: str | None = None, doi: str | None = None, pmid: str | None = None
    ) -> KnowledgeSource | None:
        wanted = {("pmcid", pmcid), ("doi", doi), ("pmid", pmid)}
        return next(
            (
                s
                for s in sorted(self.rows.values(), key=lambda s: s.uploaded_at)
                if any(value is not None and getattr(s, name) == value for name, value in wanted)
            ),
            None,
        )

    async def add_if_new(self, source: NewKnowledgeSource) -> KnowledgeSource | None:
        if self.racing is not None:
            # Another stage of the same article committed between the lookup and this insert.
            self.rows[self.racing.id] = self.racing
            self.racing = None
            return None
        taken = await self.find_by_identifiers(pmcid=source.pmcid, doi=source.doi, pmid=source.pmid)
        if taken is not None or await self.get_by_hash(source.content_hash) is not None:
            return None
        stored = KnowledgeSource(**source.model_dump())
        self.rows[stored.id] = stored
        return stored

    async def delete(self, source_id: UUID) -> bool:
        return self.rows.pop(source_id, None) is not None

    async def queue_reingest(
        self, source_id: UUID, *, requested_by: UUID
    ) -> KnowledgeSource | None:
        source = self.rows.get(source_id)
        if source is None or source.status not in (SourceStatus.INGESTED, SourceStatus.FAILED):
            return None
        queued = source.model_copy(
            update={
                "status": SourceStatus.UPLOADED,
                "error": None,
                "ingested_at": None,
                "uploaded_by_user_id": requested_by,
            }
        )
        self.rows[source_id] = queued
        return queued


class _NoClock:
    def now(self) -> datetime:
        raise AssertionError("no upload in this test")


class KnowledgeWiring(TypedDict):
    knowledge: KnowledgeSources
    sports: SportRegistry
    max_upload_mb: int


def knowledge_wiring() -> KnowledgeWiring:
    """`build_admin_router`'s knowledge arguments, for a test about the rest of the admin view."""
    registry = default_sport_registry()
    return {
        "knowledge": KnowledgeSources(
            sources=MemorySources(),
            storage=MemoryStorage(),
            clock=_NoClock(),
            max_upload_bytes=1024,
            sports=[sport.id for sport in registry.all()],
        ),
        "sports": registry,
        "max_upload_mb": 1,
    }
