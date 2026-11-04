"""Pages of the knowledge manifest for the API (#457, owner at the plan gate): a keyset on `id`
with `next` set only while more remain."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.knowledge_sources import KnowledgeSources, SourcesPage
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeFileType,
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    SourceLanguage,
    SourceType,
    content_hash,
)
from tests.knowledge_support import MemorySources, MemoryStorage

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


def _stored(source_id: UUID) -> KnowledgeSource:
    return KnowledgeSource(
        id=source_id,
        title="Deload",
        author=None,
        source_type=SourceType.ARTICLE,
        evidence_tier=EvidenceTier.ARTICLE_OR_TRANSCRIPT,
        language=SourceLanguage.EN,
        sports=("gym",),
        topics=(),
        license_note=None,
        license_id=LicenseId.CC_BY,
        license_source=LicenseSource.ADMIN,
        source_url=None,
        content_hash=content_hash(source_id.bytes),
        file_type=KnowledgeFileType.TEXT,
        file_name="deload.txt",
        size_bytes=1,
        storage_key=f"sources/{source_id}",
        uploaded_by_user_id=None,
        uploaded_at=NOW,
    )


def _service(count: int) -> tuple[KnowledgeSources, list[UUID]]:
    sources = MemorySources()
    ids = sorted((uuid4() for _ in range(count)), key=str)
    for source_id in ids:
        sources.rows[source_id] = _stored(source_id)
    service = KnowledgeSources(
        sources=sources, storage=MemoryStorage(), clock=Clock(), max_upload_bytes=1, sports=[]
    )
    return service, ids


async def test_a_full_page_with_more_behind_it_names_the_next_cursor() -> None:
    service, ids = _service(3)

    page = await service.list_page(after=None, limit=2)

    assert [s.id for s in page.sources] == ids[:2]
    assert page.next == ids[1]


@pytest.mark.parametrize("count", [0, 1, 2], ids=["empty", "short", "exactly-full"])
async def test_the_last_page_has_no_next_cursor(count: int) -> None:
    service, ids = _service(count)

    page = await service.list_page(after=None, limit=2)

    assert page == SourcesPage(sources=[_stored(i) for i in ids], next=None)


async def test_the_cursor_starts_the_page_after_it() -> None:
    service, ids = _service(3)

    page = await service.list_page(after=ids[1], limit=2)

    assert [s.id for s in page.sources] == ids[2:]
    assert page.next is None
