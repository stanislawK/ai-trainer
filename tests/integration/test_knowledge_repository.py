"""`SqlAlchemyKnowledgeSourcesRepository` on real PostgreSQL (ADR-0033, #422)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.knowledge import (
    DuplicateSourceError,
    EvidenceTier,
    KnowledgeFileType,
    LicenseId,
    LicenseSource,
    NewKnowledgeSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
    content_hash,
)
from ai_trainer.domain.users import NewUser, User, UserStatus

NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


async def _admin(factory: Callable[[], AsyncSession], sub: str) -> User:
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )


def _source(marker: str, uploader: User, **overrides: object) -> NewKnowledgeSource:
    source_id = uuid4()
    fields: dict[str, object] = {
        "id": source_id,
        "title": "Periodyzacja treningu siłowego",
        "author": None,
        "source_type": SourceType.BOOK,
        "evidence_tier": EvidenceTier.COACHING_BOOK,
        "language": SourceLanguage.PL,
        "sports": ("gym",),
        "topics": ("periodization",),
        "license_note": "Owner's copy",
        "license_id": LicenseId.OTHER,
        "license_source": LicenseSource.ADMIN,
        "source_url": None,
        "content_hash": content_hash(f"repo-{marker}-{source_id}".encode()),
        "file_type": KnowledgeFileType.EPUB,
        "file_name": "periodyzacja.epub",
        "size_bytes": 1234,
        "storage_key": f"sources/{source_id}",
        "uploaded_by_user_id": uploader.id,
        "uploaded_at": NOW,
    }
    fields.update(overrides)
    return NewKnowledgeSource.model_validate(fields)


async def _set_status(
    factory: Callable[[], AsyncSession], source_id: object, status: str, error: str | None = None
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE knowledge_sources SET status = :status, error = :error, "
                "ingested_at = :at WHERE id = :id"
            ),
            {"status": status, "error": error, "at": NOW, "id": source_id},
        )
        await session.commit()


async def test_add_stores_every_field_and_get_reads_it_back(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-add")
    new = _source("add", admin, author="A. Zając", source_url="https://example.org/p")

    stored = await repo.add(new)

    assert stored.model_dump(exclude={"error", "ingested_at"}) == new.model_dump()
    assert (stored.status, stored.error, stored.ingested_at) == (SourceStatus.UPLOADED, None, None)
    assert await repo.get(new.id) == stored
    assert await repo.get_by_hash(new.content_hash) == stored
    assert await repo.get(uuid4()) is None
    assert await repo.get_by_hash("0" * 64) is None


async def test_a_source_round_trips_its_license_and_identifiers(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    # A staged PMC paper's fields (ADR-0036, #453); identifiers are unique, so fresh per run.
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-license")
    number = uuid4().int % 10**9
    new = _source(
        "license",
        admin,
        status=SourceStatus.PROPOSED,
        file_type=KnowledgeFileType.JATS,
        license_id=LicenseId.CC_BY_NC_ND,
        license_source=LicenseSource.PMC,
        license_note=None,
        license_url="https://creativecommons.org/licenses/by-nc-nd/4.0/",
        copyright_statement="© 2025 The Authors",
        attribution="A. Zając, Deload timing. Source: NLM PMC",
        pmcid=f"PMC{number}",
        doi=f"10.1000/deload.{number}",
        pmid=str(number),
        isbn=f"978{number:010d}",
    )

    stored = await repo.add(new)

    assert stored.model_dump(exclude={"error", "ingested_at"}) == new.model_dump()
    assert await repo.get(new.id) == stored


async def test_a_second_source_with_the_same_hash_is_a_duplicate(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-dup")
    first = await repo.add(_source("dup", admin))

    with pytest.raises(DuplicateSourceError):
        await repo.add(_source("dup-2", admin, content_hash=first.content_hash))
    assert await repo.get_by_hash(first.content_hash) == first


async def test_list_all_is_newest_first(db_session_factory: Callable[[], AsyncSession]) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-list")
    older = await repo.add(_source("old", admin, uploaded_at=NOW - timedelta(days=1)))
    newer = await repo.add(_source("new", admin, uploaded_at=NOW + timedelta(days=365)))

    listed = [s.id for s in await repo.list_all()]

    assert listed.index(newer.id) < listed.index(older.id)


async def test_delete_removes_only_that_source(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-delete")
    gone, kept = await repo.add(_source("gone", admin)), await repo.add(_source("kept", admin))

    assert await repo.delete(gone.id) is True
    assert await repo.delete(gone.id) is False
    assert await repo.get(gone.id) is None
    assert await repo.get(kept.id) == kept


@pytest.mark.parametrize("status", ["ingested", "failed"])
async def test_queue_reingest_sets_an_ingested_or_failed_source_back_to_uploaded(
    db_session_factory: Callable[[], AsyncSession], status: str
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, f"knowledge-repo-re-{status}")
    other = await _admin(db_session_factory, f"knowledge-repo-re-other-{status}")
    source = await repo.add(_source(f"re-{status}", admin))
    await _set_status(db_session_factory, source.id, status, error="no_text")

    queued = await repo.queue_reingest(source.id, requested_by=other.id)

    assert queued is not None
    assert (queued.status, queued.error, queued.ingested_at) == (SourceStatus.UPLOADED, None, None)
    assert queued.uploaded_by_user_id == other.id
    assert await repo.get(source.id) == queued


@pytest.mark.parametrize("status", ["uploaded", "ingesting"])
async def test_queue_reingest_leaves_a_waiting_source_alone(
    db_session_factory: Callable[[], AsyncSession], status: str
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, f"knowledge-repo-wait-{status}")
    source = await repo.add(_source(f"wait-{status}", admin))
    await _set_status(db_session_factory, source.id, status)
    before = await repo.get(source.id)

    assert await repo.queue_reingest(source.id, requested_by=admin.id) is None
    assert await repo.get(source.id) == before
    assert await repo.queue_reingest(uuid4(), requested_by=admin.id) is None


async def test_a_source_outlives_the_account_that_uploaded_it(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-orphan")
    source = await repo.add(_source("orphan", admin))

    async with db_session_factory() as session:
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin.id})
        await session.commit()

    kept = await repo.get(source.id)
    assert kept is not None
    assert kept.uploaded_by_user_id is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("evidence_tier", "influencer"),
        ("source_type", "tweet"),
        ("license_note", "  "),
        ("sports", "{}"),
        ("status", "lost"),
        ("file_type", "docx"),
        ("language", "de"),
    ],
)
async def test_the_table_refuses_values_outside_its_sets(
    db_session_factory: Callable[[], AsyncSession], column: str, value: str
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, f"knowledge-repo-check-{column}")
    source = await repo.add(_source(f"check-{column}", admin))

    # A literal, so Postgres reads '{}' as an empty array for `sports`; the values are constants.
    async with db_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(f"UPDATE knowledge_sources SET {column} = '{value}' WHERE id = :id"),
                {"id": source.id},
            )
