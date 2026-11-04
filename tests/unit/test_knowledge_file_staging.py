"""Staging files for upload by presigned PUT (ADR-0036, #460), with in-memory sources and storage.

The PUT itself is the client's: a test stands in for it by writing the object at the key the
link names."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from ai_trainer.application.knowledge_file_staging import (
    LINK_LIFETIME,
    FileStaging,
    ObjectMissing,
    RegisterCovered,
    Registered,
    RegisterRefused,
    StageCovered,
    StageRefused,
    UploadNotFound,
    UploadReady,
    staged_key,
    upload_key,
)
from ai_trainer.application.knowledge_sources import StorageUnavailable
from ai_trainer.domain.knowledge import (
    KnowledgeFileType,
    KnowledgeSource,
    LicenseId,
    NewKnowledgeSource,
    SourceStatus,
    UploadProblem,
    content_hash,
)
from ai_trainer.domain.object_storage import ObjectStorageOperation
from tests.knowledge_support import MemorySources, MemoryStorage, epub_bytes, pdf_bytes

NOW = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
ADMIN = UUID(int=42)
MAX_BYTES = 1024


class Clock:
    def now(self) -> datetime:
        return NOW


def template(**overrides: JsonValue) -> dict[str, JsonValue]:
    """#456's template for a book, as a token client sends it (CSL-JSON keys)."""
    fields: dict[str, JsonValue] = {
        "type": "book",
        "title": "Science and Practice of Strength Training",
        "author": [{"family": "Zatsiorsky", "given": "Vladimir"}],
        "ISBN": "9781492592006",
        "language": "en",
        "evidence_tier": "coaching_book",
        "source_type": "book",
        "sports": ["gym"],
        "topics": ["Periodization"],
        "licence": {
            "license_id": "other",
            "license_source": "admin",
            "attribution": "Zatsiorsky, Kraemer, Fry, Human Kinetics 2021",
            "license_note": "The owner's own copy, for the app's non-commercial use.",
        },
    }
    fields.update(overrides)
    return fields


def staging(
    sources: MemorySources | None = None, storage: MemoryStorage | None = None
) -> tuple[FileStaging, MemorySources, MemoryStorage]:
    sources = sources or MemorySources()
    storage = storage or MemoryStorage()
    return (
        FileStaging(
            sources=sources,
            storage=storage,
            clock=Clock(),
            max_upload_bytes=MAX_BYTES,
            sports=["gym", "climbing"],
        ),
        sources,
        storage,
    )


async def ready(
    service: FileStaging, file_name: str = "zatsiorsky.pdf", **fields: JsonValue
) -> UUID:
    outcome = await service.stage(file_name, template(**fields))
    assert isinstance(outcome, UploadReady), outcome
    return outcome.upload_id


def existing(**overrides: object) -> KnowledgeSource:
    fields: dict[str, object] = {
        "id": uuid4(),
        "title": "Already here",
        "author": None,
        "topics": (),
        "license_note": None,
        "source_url": None,
        "source_type": "book",
        "evidence_tier": "coaching_book",
        "language": "en",
        "sports": ("gym",),
        "license_id": "cc-by",
        "license_source": "admin",
        "attribution": "Someone, CC BY 4.0",
        "content_hash": content_hash(uuid4().bytes),
        "file_type": "pdf",
        "file_name": "here.pdf",
        "size_bytes": 10,
        "storage_key": f"sources/{uuid4()}",
        "status": SourceStatus.INGESTED,
        "uploaded_by_user_id": ADMIN,
        "uploaded_at": NOW - timedelta(days=1),
    }
    fields.update(overrides)
    return KnowledgeSource(**NewKnowledgeSource.model_validate(fields).model_dump())


# --- stage ---------------------------------------------------------------------------------


async def test_stage_issues_an_hour_long_put_link_for_the_upload_and_keeps_its_metadata() -> None:
    service, _, storage = staging()

    outcome = await service.stage("zatsiorsky.pdf", template())

    assert isinstance(outcome, UploadReady)
    assert storage.put_links == [(upload_key(outcome.upload_id), LINK_LIFETIME)]
    assert timedelta(hours=1) == LINK_LIFETIME
    assert outcome.expires_at == NOW + LINK_LIFETIME
    assert "X-Amz-Signature" in outcome.put_url
    staged = json.loads(storage.objects[staged_key(outcome.upload_id)])
    assert staged["file_name"] == "zatsiorsky.pdf"
    assert staged["source"]["title"] == "Science and Practice of Strength Training"
    assert storage.content_types[staged_key(outcome.upload_id)] == "application/json"


async def test_staging_again_issues_a_new_upload_and_link() -> None:
    service, _, storage = staging()

    first = await service.stage("zatsiorsky.pdf", template())
    second = await service.stage("zatsiorsky.pdf", template())

    assert isinstance(first, UploadReady)
    assert isinstance(second, UploadReady)
    assert first.upload_id != second.upload_id
    assert first.put_url != second.put_url
    assert len(storage.put_links) == 2


async def test_an_invalid_template_or_unlisted_file_name_is_refused_per_field() -> None:
    service, _, storage = staging()

    outcome = await service.stage("notes.docx", template(title="  ", sports=["rowing"]))

    assert outcome == StageRefused(
        problems={"file_name": "unlisted_type", "title": "string_too_short", "sports": "unknown"}
    )
    assert storage.objects == {}
    assert storage.put_links == []


async def test_a_template_key_outside_the_model_is_refused() -> None:
    service, _, _ = staging()

    outcome = await service.stage("a.pdf", template(publisher="Human Kinetics"))

    assert outcome == StageRefused(problems={"publisher": "extra_forbidden"})


@pytest.mark.parametrize(
    "identifier",
    [{"ISBN": "9781492592006"}, {"DOI": "10.5555/strength.2021"}],
    ids=["isbn", "doi"],
)
async def test_a_source_with_the_same_identifier_is_covered_and_no_link_is_issued(
    identifier: dict[str, str],
) -> None:
    sources = MemorySources()
    here = existing(isbn="9781492592006", doi="10.5555/strength.2021")
    sources.rows[here.id] = here
    service, _, storage = staging(sources)

    outcome = await service.stage("a.pdf", template(**{"ISBN": None, **identifier}))

    assert outcome == StageCovered(source=here)
    assert storage.put_links == []
    assert storage.objects == {}


async def test_staging_without_storage_settings_is_refused() -> None:
    service = FileStaging(
        sources=MemorySources(),
        storage=None,
        clock=Clock(),
        max_upload_bytes=MAX_BYTES,
        sports=["gym"],
    )

    assert await service.stage("a.pdf", template()) == StorageUnavailable(reason="not_configured")
    assert await service.register(ADMIN, uuid4()) == StorageUnavailable(reason="not_configured")


@pytest.mark.parametrize("failing", ["put", "presign"], ids=["metadata", "link"])
async def test_a_storage_failure_while_staging_is_reported(
    failing: ObjectStorageOperation,
) -> None:
    storage = MemoryStorage()
    storage.failing.add(failing)
    service, _, _ = staging(storage=storage)

    assert await service.stage("a.pdf", template()) == StorageUnavailable(reason="failed")


# --- register ------------------------------------------------------------------------------


async def test_register_stores_a_proposed_source_with_its_type_size_and_licence() -> None:
    service, sources, storage = staging()
    upload_id = await ready(service)
    data = pdf_bytes("register")
    storage.objects[upload_key(upload_id)] = data

    outcome = await service.register(ADMIN, upload_id)

    assert isinstance(outcome, Registered)
    source = outcome.source
    assert source.id == upload_id
    assert source.status is SourceStatus.PROPOSED
    assert source.file_type is KnowledgeFileType.PDF
    assert source.size_bytes == len(data)
    assert source.file_name == "zatsiorsky.pdf"
    assert source.storage_key == upload_key(upload_id)
    assert source.content_hash == content_hash(data)
    assert source.isbn == "9781492592006"
    assert source.author == "Vladimir Zatsiorsky"
    assert source.license_id is LicenseId.OTHER
    assert source.license_note == "The owner's own copy, for the app's non-commercial use."
    assert source.uploaded_by_user_id == ADMIN
    assert source.uploaded_at == NOW
    assert sources.rows[upload_id] == source
    assert upload_key(upload_id) in storage.objects
    assert staged_key(upload_id) not in storage.objects


async def test_a_template_without_a_licence_registers_proposed_with_none() -> None:
    service, _, storage = staging()
    upload_id = await ready(service, "book.epub", licence=None)
    storage.objects[upload_key(upload_id)] = epub_bytes("no-licence")

    outcome = await service.register(ADMIN, upload_id)

    assert isinstance(outcome, Registered)
    assert outcome.source.status is SourceStatus.PROPOSED
    assert outcome.source.file_type is KnowledgeFileType.EPUB
    assert outcome.source.license_id is None


async def test_registering_again_answers_with_the_source_and_deletes_nothing() -> None:
    service, _, storage = staging()
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = pdf_bytes("again")
    first = await service.register(ADMIN, upload_id)

    second = await service.register(ADMIN, upload_id)

    assert isinstance(first, Registered)
    assert second == first
    assert upload_key(upload_id) in storage.objects


async def test_registering_a_file_already_stored_reports_it_and_deletes_the_new_object() -> None:
    data = pdf_bytes("duplicate")
    sources = MemorySources()
    here = existing(content_hash=content_hash(data))
    sources.rows[here.id] = here
    service, _, storage = staging(sources)
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = data

    outcome = await service.register(ADMIN, upload_id)

    assert outcome == RegisterCovered(source=here)
    assert upload_key(upload_id) not in storage.objects
    assert staged_key(upload_id) not in storage.objects
    assert upload_id not in sources.rows


async def test_a_source_staged_by_identifier_since_the_stage_covers_the_upload() -> None:
    service, sources, storage = staging()
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = pdf_bytes("identifier-race")
    here = existing(isbn="9781492592006")
    sources.rows[here.id] = here

    outcome = await service.register(ADMIN, upload_id)

    assert outcome == RegisterCovered(source=here)
    assert upload_key(upload_id) not in storage.objects


async def test_a_duplicate_stored_between_the_lookup_and_the_insert_is_covered() -> None:
    service, sources, storage = staging()
    upload_id = await ready(service)
    data = pdf_bytes("insert-race")
    storage.objects[upload_key(upload_id)] = data
    sources.racing = existing(content_hash=content_hash(data))

    outcome = await service.register(ADMIN, upload_id)

    assert isinstance(outcome, RegisterCovered)
    assert upload_key(upload_id) not in storage.objects


async def test_a_second_register_of_one_upload_racing_the_first_deletes_nothing() -> None:
    service, sources, storage = staging()
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = pdf_bytes("register-race")
    winner = existing(id=upload_id, storage_key=upload_key(upload_id), status=SourceStatus.PROPOSED)
    # The other register's insert commits between this one's lookup and its insert.
    sources.racing = winner

    outcome = await service.register(ADMIN, upload_id)

    assert outcome == Registered(source=winner)
    assert upload_key(upload_id) in storage.objects


@pytest.mark.parametrize(
    ("file_name", "data", "problem"),
    [
        ("zatsiorsky.pdf", b"PK\x03\x04 not a pdf", UploadProblem.UNLISTED_TYPE),
        ("zatsiorsky.pdf", b"%PDF-" + b"x" * MAX_BYTES, UploadProblem.TOO_LARGE),
        ("zatsiorsky.pdf", b"", UploadProblem.MISSING),
    ],
    ids=["wrong_type", "too_large", "empty"],
)
async def test_a_wrong_type_oversized_or_empty_object_is_refused_and_deleted(
    file_name: str, data: bytes, problem: UploadProblem
) -> None:
    service, sources, storage = staging()
    upload_id = await ready(service, file_name)
    storage.objects[upload_key(upload_id)] = data

    outcome = await service.register(ADMIN, upload_id)

    assert outcome == RegisterRefused(problem=problem)
    assert upload_key(upload_id) not in storage.objects
    assert staged_key(upload_id) not in storage.objects
    assert sources.rows == {}


async def test_an_oversized_object_is_refused_without_reading_its_bytes() -> None:
    service, _, storage = staging()
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = b"%PDF-" + b"x" * MAX_BYTES

    outcome = await service.register(ADMIN, upload_id)

    assert outcome == RegisterRefused(problem=UploadProblem.TOO_LARGE)
    assert upload_key(upload_id) not in storage.reads


async def test_registering_before_the_put_reports_the_object_missing_and_keeps_the_stage() -> None:
    service, sources, storage = staging()
    upload_id = await ready(service)

    assert await service.register(ADMIN, upload_id) == ObjectMissing()
    assert staged_key(upload_id) in storage.objects
    assert sources.rows == {}

    storage.objects[upload_key(upload_id)] = pdf_bytes("late-put")
    assert isinstance(await service.register(ADMIN, upload_id), Registered)


async def test_an_upload_no_stage_issued_is_not_found() -> None:
    service, _, storage = staging()
    stray = uuid4()
    storage.objects[upload_key(stray)] = pdf_bytes("stray")

    assert await service.register(ADMIN, stray) == UploadNotFound()
    assert upload_key(stray) in storage.objects


@pytest.mark.parametrize("failing", ["get", "head"], ids=["metadata", "size"])
async def test_a_storage_failure_while_registering_is_reported_and_keeps_the_stage(
    failing: ObjectStorageOperation,
) -> None:
    service, _, storage = staging()
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = pdf_bytes("storage-failure")
    storage.failing.add(failing)

    assert await service.register(ADMIN, upload_id) == StorageUnavailable(reason="failed")
    assert staged_key(upload_id) in storage.objects
    assert upload_key(upload_id) in storage.objects


async def test_a_failed_cleanup_still_reports_the_refusal() -> None:
    service, _, storage = staging()
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = b"not a pdf at all"
    storage.failing.add("delete")

    outcome = await service.register(ADMIN, upload_id)

    assert outcome == RegisterRefused(problem=UploadProblem.UNLISTED_TYPE)


async def test_a_clash_with_a_source_deleted_since_keeps_the_upload_for_a_retry() -> None:
    service, sources, storage = staging()
    upload_id = await ready(service)
    storage.objects[upload_key(upload_id)] = pdf_bytes("vanished-clash")
    vanished = existing()
    # The insert clashed with a source the next lookups no longer find.
    sources.racing = vanished

    outcome = await service.register(ADMIN, upload_id)
    sources.rows.pop(vanished.id)

    assert outcome == StorageUnavailable(reason="failed")
    assert upload_key(upload_id) in storage.objects
    assert staged_key(upload_id) in storage.objects
    assert isinstance(await service.register(ADMIN, upload_id), Registered)
