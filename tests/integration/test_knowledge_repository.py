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
from ai_trainer.domain.source_template import LicenceTerms
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

    assert (
        stored.model_dump(
            exclude={"error", "ingested_at", "chunk_count", "embedding_model", "ingest_takeovers"}
        )
        == new.model_dump()
    )
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

    assert (
        stored.model_dump(
            exclude={"error", "ingested_at", "chunk_count", "embedding_model", "ingest_takeovers"}
        )
        == new.model_dump()
    )
    assert await repo.get(new.id) == stored


async def test_a_detecting_source_round_trips_with_no_licence(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    # A form upload left to licence detection (the #454 amendment, #456): NULLs, not defaults.
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-detecting")
    new = _source(
        "detecting",
        admin,
        status=SourceStatus.DETECTING,
        license_id=None,
        license_source=None,
        license_note=None,
    )

    stored = await repo.add(new)

    assert (stored.status, stored.license_id, stored.license_source) == (
        SourceStatus.DETECTING,
        None,
        None,
    )
    assert await repo.get(new.id) == stored


async def test_a_detected_licence_round_trips_what_was_matched(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-detected")
    new = _source(
        "detected",
        admin,
        status=SourceStatus.PROPOSED,
        license_id=LicenseId.CC_BY_SA,
        license_source=LicenseSource.FILE,
        license_note=None,
        attribution="K. Marsh, Hypertrophy handbook, ch. 2, CC BY-SA 4.0",
        license_matched_line="Licensed under CC BY-SA 4.0.",
        license_locator="page 2",
        license_detection_reason=None,
    )

    stored = await repo.add(new)

    assert (
        stored.model_dump(
            exclude={"error", "ingested_at", "chunk_count", "embedding_model", "ingest_takeovers"}
        )
        == new.model_dump()
    )
    assert stored.licence_flag is not None


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


async def test_list_page_orders_by_id_after_the_cursor(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The knowledge API's keyset (#457). Other tests' rows may share the table, so only the
    order of this test's own rows is asserted."""
    repository = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-page")
    added = sorted(
        [(await repository.add(_source(f"page-{n}", admin))).id for n in range(3)], key=str
    )

    first = await repository.list_page(after=None, limit=500)
    after_first = await repository.list_page(after=added[0], limit=500)
    one = await repository.list_page(after=added[0], limit=1)

    assert [s.id for s in first if s.id in added] == added
    assert [s.id for s in after_first if s.id in added] == added[1:]
    assert all(str(s.id) > str(added[0]) for s in after_first)
    assert len(one) == 1
    assert [str(s.id) for s in first] == sorted(str(s.id) for s in first)


def _unique_pmcid() -> str:
    # Unique per run: a second insert of an identifier another test's open transaction holds
    # would wait for it (knowledge_support.py).
    return f"PMC{uuid4().int % 10**12}"


async def test_find_by_identifiers_matches_any_one_of_pmcid_doi_and_pmid(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-identifiers")
    pmcid, number = _unique_pmcid(), str(uuid4().int % 10**9)
    doi = f"10.9999/repo.{number}"
    stored = await repo.add(_source("ids", admin, pmcid=pmcid, doi=doi, pmid=number))
    await repo.add(_source("other", admin))

    assert (await repo.find_by_identifiers(pmcid=pmcid)) == stored
    assert (await repo.find_by_identifiers(pmcid=_unique_pmcid(), doi=doi)) == stored
    assert (await repo.find_by_identifiers(pmid=number)) == stored
    assert await repo.find_by_identifiers(pmcid=_unique_pmcid(), doi="10.9999/none") is None
    assert await repo.find_by_identifiers() is None


async def test_add_if_new_stores_a_source_and_skips_any_unique_conflict(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-add-if-new")
    pmcid = _unique_pmcid()
    first = _source("first", admin, pmcid=pmcid, status=SourceStatus.PROPOSED)

    stored = await repo.add_if_new(first)

    assert stored is not None
    assert stored.id == first.id
    assert stored.status is SourceStatus.PROPOSED
    # Another hash, the same PMC ID: skipped, not an error that aborts the transaction.
    assert await repo.add_if_new(_source("second", admin, pmcid=pmcid)) is None
    # The same hash: skipped too.
    assert await repo.add_if_new(first.model_copy(update={"id": uuid4(), "pmcid": None})) is None
    assert await repo.get(first.id) == stored


async def test_find_by_identifiers_matches_an_isbn(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    admin = await _admin(db_session_factory, "knowledge-repo-isbn")
    isbn = f"978{uuid4().int % 10**10:010d}"
    stored = await repo.add(_source("book", admin, isbn=isbn))

    assert (await repo.find_by_identifiers(isbn=isbn)) == stored
    assert await repo.find_by_identifiers(isbn=f"979{uuid4().int % 10**10:010d}") is None


# --- #461: deciding on proposals and setting a proposal's licence -----------------------------


async def test_approve_and_reject_change_only_licensed_proposals(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    admin = await _admin(db_session_factory, "repo-decide")
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    licensed = await repo.add(_source("decide-a", admin, status=SourceStatus.PROPOSED))
    other = await repo.add(_source("decide-b", admin, status=SourceStatus.PROPOSED))
    unlicensed = await repo.add(
        _source(
            "decide-c",
            admin,
            status=SourceStatus.PROPOSED,
            license_id=None,
            license_source=None,
            license_note=None,
        )
    )
    ingested = await repo.add(_source("decide-d", admin))
    await _set_status(db_session_factory, ingested.id, "ingested")
    approver = await _admin(db_session_factory, "repo-decide-approver")

    approved = await repo.decide_proposals(
        [licensed.id, unlicensed.id, ingested.id, uuid4()],
        status=SourceStatus.UPLOADED,
        decided_by=approver.id,
    )
    rejected = await repo.decide_proposals(
        [other.id, licensed.id], status=SourceStatus.REJECTED, decided_by=approver.id
    )

    assert list(approved) == [licensed.id]
    assert list(rejected) == [other.id]
    rows = {s.id: s for s in await repo.get_many([licensed.id, other.id, unlicensed.id])}
    assert rows[licensed.id].status is SourceStatus.UPLOADED
    assert rows[licensed.id].uploaded_by_user_id == approver.id
    assert rows[other.id].status is SourceStatus.REJECTED
    assert rows[other.id].uploaded_by_user_id == admin.id
    assert rows[unlicensed.id].status is SourceStatus.PROPOSED
    assert (await repo.get(ingested.id)).status is SourceStatus.INGESTED  # type: ignore[union-attr]


async def test_decide_proposals_refuses_a_status_other_than_uploaded_or_rejected(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)

    with pytest.raises(ValueError, match="uploaded or rejected"):
        await repo.decide_proposals([uuid4()], status=SourceStatus.INGESTED, decided_by=uuid4())


async def test_decide_proposals_with_no_ids_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)

    assert await repo.decide_proposals([], status=SourceStatus.REJECTED, decided_by=uuid4()) == []


async def test_get_many_skips_unknown_ids(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    admin = await _admin(db_session_factory, "repo-get-many")
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    stored = await repo.add(_source("get-many", admin))

    assert [s.id for s in await repo.get_many([stored.id, uuid4()])] == [stored.id]
    assert await repo.get_many([]) == []


async def test_set_licence_writes_only_a_proposed_source(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    admin = await _admin(db_session_factory, "repo-set-licence")
    repo = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    waiting = await repo.add(
        _source(
            "set-licence-a",
            admin,
            status=SourceStatus.PROPOSED,
            license_id=None,
            license_source=None,
            license_note=None,
            license_detection_reason="none_found",
        )
    )
    uploaded = await repo.add(_source("set-licence-b", admin))
    terms = LicenceTerms(
        license_id=LicenseId.OTHER,
        license_source=LicenseSource.ADMIN,
        license_url="https://example.org/permission",
        copyright_statement="© 2025 R. Lind",
        attribution="R. Lind, used with permission",
        license_note="Written permission, 28 Oct 2026.",
    )

    updated = await repo.set_licence(waiting.id, terms)
    refused = await repo.set_licence(uploaded.id, terms)

    assert updated is not None
    assert (updated.status, updated.license_id, updated.license_source) == (
        SourceStatus.PROPOSED,
        LicenseId.OTHER,
        LicenseSource.ADMIN,
    )
    assert updated.license_url == "https://example.org/permission"
    assert updated.copyright_statement == "© 2025 R. Lind"
    assert updated.attribution == "R. Lind, used with permission"
    assert updated.license_note == "Written permission, 28 Oct 2026."
    # Detection's leftovers no longer describe the licence: the admin set it.
    assert updated.license_detection_reason is None
    assert refused is None
    assert (await repo.get(uploaded.id)).license_id is LicenseId.OTHER  # type: ignore[union-attr]
    # A proposal that has a licence keeps it: Set licence is for one that has none.
    assert await repo.set_licence(waiting.id, terms.model_copy(update={"attribution": "x"})) is None
    assert (await repo.get(waiting.id)).attribution == "R. Lind, used with permission"  # type: ignore[union-attr]
