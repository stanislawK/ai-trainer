"""The upload rules and the upload service with fakes (ADR-0033, #422)."""

import hashlib
import io
import zipfile
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_trainer.application.knowledge_sources import (
    AlreadyStored,
    KnowledgeSources,
    NotReingestable,
    ReingestQueued,
    SourceDeleted,
    SourceNotFound,
    SourceStored,
    StorageUnavailable,
    UploadRefused,
)
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeFileType,
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
    UploadProblem,
    check_file,
)
from ai_trainer.domain.knowledge_form import UploadAnswers, UploadField, read_details
from tests.knowledge_support import (
    MemorySources,
    MemoryStorage,
    answers,
    epub_bytes,
    pdf_bytes,
)

NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
ADMIN = UUID("00000000-0000-4000-8000-00000000a422")
SPORTS = ("climbing", "gym", "cycling")
MAX_BYTES = 1024
_JATS = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange '
    b'DTD v1.4//EN" "JATS-archivearticle1-4.dtd">\n'
    b'<article article-type="research-article"><body><p>Hi</p></body></article>'
)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class File:
    """What the route hands over: Starlette's `UploadFile` has the same two members."""

    def __init__(self, filename: str | None, data: bytes) -> None:
        self.filename = filename
        self._data = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._data.read(size)


def _answers(**overrides: str | list[str]) -> UploadAnswers:
    fields = answers(**overrides)
    sports = fields.pop("sports")
    assert isinstance(sports, list)
    return UploadAnswers(
        **{k: v for k, v in fields.items() if isinstance(v, str)}, sports=tuple(sports)
    )


def _service(
    sources: MemorySources | None = None, storage: MemoryStorage | None = None
) -> KnowledgeSources:
    return KnowledgeSources(
        sources=sources or MemorySources(),
        storage=storage,
        clock=FixedClock(),
        max_upload_bytes=MAX_BYTES,
        sports=SPORTS,
    )


def _stored(status: SourceStatus, data: bytes, error: str | None = None) -> KnowledgeSource:
    source_id = uuid4()
    return KnowledgeSource(
        id=source_id,
        title="Hangboard protocols (scan)",
        author="T. Weber",
        source_type=SourceType.BOOK,
        evidence_tier=EvidenceTier.COACHING_BOOK,
        language=SourceLanguage.EN,
        sports=("climbing",),
        topics=(),
        license_note="Owner's copy",
        source_url=None,
        license_id=LicenseId.OTHER,
        license_source=LicenseSource.ADMIN,
        content_hash=hashlib.sha256(data).hexdigest(),
        file_type=KnowledgeFileType.PDF,
        file_name="hangboard.pdf",
        size_bytes=len(data),
        storage_key=f"sources/{source_id}",
        status=status,
        error=error,
        uploaded_by_user_id=ADMIN,
        uploaded_at=NOW,
        ingested_at=NOW if status is SourceStatus.INGESTED else None,
    )


# --- The identifiers (ADR-0036, #453) -------------------------------------------------------


def _with(**fields: str) -> KnowledgeSource:
    return KnowledgeSource.model_validate(
        {**_stored(SourceStatus.UPLOADED, b"ids").model_dump(), **fields}
    )


def test_a_doi_is_stored_lowercased_so_any_spelling_is_one_source() -> None:
    assert _with(doi=" 10.1136/BJSPORTS-2019-101234 ").doi == "10.1136/bjsports-2019-101234"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("doi", "https://doi.org/10.1136/bjsports"),
        ("doi", "bjsports-2019"),
        ("pmcid", "13692514"),
        ("pmcid", "pmc13692514"),
        ("pmid", "PMID41234567"),
    ],
)
def test_a_malformed_identifier_is_refused_before_the_table_sees_it(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _with(**{field: value})


def test_identifiers_are_optional() -> None:
    source = _stored(SourceStatus.UPLOADED, b"none")
    assert (source.pmcid, source.doi, source.pmid, source.isbn) == (None,) * 4


# --- The file ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("paper.pdf", pdf_bytes("a"), KnowledgeFileType.PDF),
        ("Paper.PDF", pdf_bytes("b"), KnowledgeFileType.PDF),
        ("book.epub", epub_bytes("c"), KnowledgeFileType.EPUB),
        ("notes.md", b"# Deload\n", KnowledgeFileType.MARKDOWN),
        ("notes.markdown", b"# Deload\n", KnowledgeFileType.MARKDOWN),
        ("talk.txt", "Zażółć gęślą jaźń".encode(), KnowledgeFileType.TEXT),
        ("talk.vtt", b"\xef\xbb\xbfWEBVTT\n\n00:11.000 --> 00:13.000\nHi\n", KnowledgeFileType.VTT),
        ("talk.srt", b"1\n00:00:33,843 --> 00:00:38,097\nHi\n", KnowledgeFileType.SRT),
        ("paper.xml", _JATS, KnowledgeFileType.JATS),
        ("PMC1.XML", b"<pmc-articleset><article/></pmc-articleset>", KnowledgeFileType.JATS),
        ("bom.xml", b"\xef\xbb\xbf" + _JATS, KnowledgeFileType.JATS),
    ],
    ids=[
        "pdf",
        "pdf-upper",
        "epub",
        "md",
        "markdown",
        "txt-polish",
        "vtt-bom",
        "srt",
        "jats",
        "jats-in-a-set-upper",
        "jats-bom",
    ],
)
def test_a_listed_file_type_is_accepted(
    name: str, data: bytes, expected: KnowledgeFileType
) -> None:
    assert check_file(name, data, max_bytes=MAX_BYTES) is expected


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("notes.docx", b"PK\x03\x04 not listed"),
        ("noextension", b"plain"),
        ("fake.pdf", b"<html>not a pdf</html>"),
        ("fake.epub", b"PK\x03\x04 a zip but no mimetype"),
        ("binary.txt", b"\x00\x01\x02"),
        ("latin1.md", "café".encode("latin-1")),
        ("captions.vtt", b"1\n00:00:01,000 --> 00:00:02,000\nnot webvtt\n"),
        ("feed.xml", b"<rss><channel><title>Not JATS</title></channel></rss>"),
        ("meta.xml", b"<article-meta><title-group/></article-meta>"),
        ("latin1.xml", b"<article>caf\xe9</article>"),
    ],
    ids=[
        "docx",
        "no-extension",
        "pdf-not-pdf",
        "epub-not-epub",
        "nul-bytes",
        "not-utf8",
        "vtt-header",
        "xml-not-jats",
        "article-meta-is-not-article",
        "jats-not-utf8",
    ],
)
def test_an_unlisted_or_mislabelled_file_is_refused(name: str, data: bytes) -> None:
    assert check_file(name, data, max_bytes=MAX_BYTES) is UploadProblem.UNLISTED_TYPE


def test_an_xml_file_whose_article_starts_past_the_header_window_is_refused() -> None:
    late = b" " * 20_000 + b"<article/>"

    assert check_file("late.xml", late, max_bytes=100_000) is UploadProblem.UNLISTED_TYPE


def test_an_epub_whose_first_entry_is_not_its_mimetype_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("OEBPS/chapter.xhtml", "<html/>")
        archive.writestr("mimetype", "application/epub+zip")
    assert check_file("book.epub", buffer.getvalue(), max_bytes=MAX_BYTES) is (
        UploadProblem.UNLISTED_TYPE
    )


def test_a_file_over_the_limit_is_too_large_whatever_its_type() -> None:
    assert check_file("big.docx", b"x" * (MAX_BYTES + 1), max_bytes=MAX_BYTES) is (
        UploadProblem.TOO_LARGE
    )
    assert check_file("ok.txt", b"x" * MAX_BYTES, max_bytes=MAX_BYTES) is KnowledgeFileType.TEXT


@pytest.mark.parametrize("name", [None, ""], ids=["no-name", "blank-name"])
def test_no_file_or_an_empty_one_is_missing(name: str | None) -> None:
    assert check_file(name, b"", max_bytes=MAX_BYTES) is UploadProblem.MISSING
    assert check_file("empty.pdf", b"", max_bytes=MAX_BYTES) is UploadProblem.MISSING


# --- The licence a stored source needs (ADR-0036, the #454 amendment) ----------------------


@pytest.mark.parametrize("status", [SourceStatus.DETECTING, SourceStatus.PROPOSED])
def test_a_source_waiting_for_its_licence_may_have_none(status: SourceStatus) -> None:
    source = _with(status=status, license_id=None, license_source=None)  # type: ignore[arg-type]  # None is the point
    assert source.license_id is None


@pytest.mark.parametrize(
    "status",
    [SourceStatus.UPLOADED, SourceStatus.INGESTING, SourceStatus.INGESTED, SourceStatus.FAILED],
)
def test_a_source_past_approval_needs_a_licence(status: SourceStatus) -> None:
    with pytest.raises(ValidationError, match="needs a licence"):
        _with(status=status, license_id=None, license_source=None)  # type: ignore[arg-type]  # None is the point


def test_a_licence_and_its_source_are_set_together() -> None:
    with pytest.raises(ValidationError, match="set together"):
        _with(status=SourceStatus.PROPOSED, license_source=None)  # type: ignore[arg-type]  # None is the point


def test_a_stored_other_licence_needs_its_note() -> None:
    with pytest.raises(ValidationError, match="needs a note"):
        _with(license_note=None)  # type: ignore[arg-type]  # None is the point


# --- The details ---------------------------------------------------------------------------


def test_complete_answers_read_as_details_with_topics_tidied() -> None:
    template = read_details(
        _answers(topics=" Deload, recovery ,deload,, Sleep ", author="  "), known_sports=SPORTS
    )
    assert not isinstance(template, dict)
    details = template.details()
    assert details.title == "Deload timing in resistance training"
    assert details.author is None
    assert details.source_type is SourceType.RESEARCH_PAPER
    assert details.evidence_tier is EvidenceTier.PEER_REVIEWED
    assert details.language is SourceLanguage.EN
    assert details.topics == ("deload", "recovery", "sleep")
    assert details.sports == ("gym",)
    assert details.source_url == "https://example.org/deload"


def test_optional_fields_may_be_blank() -> None:
    template = read_details(_answers(author="", topics="", source_url=""), known_sports=SPORTS)
    assert not isinstance(template, dict)
    details = template.details()
    assert (details.author, details.topics, details.source_url) == (None, (), None)


@pytest.mark.parametrize(
    ("overrides", "field", "problem"),
    [
        ({"evidence_tier": ""}, UploadField.EVIDENCE_TIER, UploadProblem.MISSING),
        ({"evidence_tier": "influencer"}, UploadField.EVIDENCE_TIER, UploadProblem.UNKNOWN),
        ({"evidence_tier": "catalogue"}, UploadField.EVIDENCE_TIER, UploadProblem.UNKNOWN),
        (
            {"license_id": "other", "license_note": "   "},
            UploadField.LICENSE_NOTE,
            UploadProblem.MISSING,
        ),
        ({"license_id": ""}, UploadField.LICENSE_ID, UploadProblem.MISSING),
        ({"license_id": "cc-by-4.0"}, UploadField.LICENSE_ID, UploadProblem.UNKNOWN),
        ({"attribution": " "}, UploadField.ATTRIBUTION, UploadProblem.MISSING),
        ({"license_url": "cc.org/by"}, UploadField.LICENSE_URL, UploadProblem.NOT_A_URL),
        ({"title": ""}, UploadField.TITLE, UploadProblem.MISSING),
        ({"source_type": ""}, UploadField.SOURCE_TYPE, UploadProblem.MISSING),
        ({"source_type": "catalogue"}, UploadField.SOURCE_TYPE, UploadProblem.UNKNOWN),
        ({"language": "de"}, UploadField.LANGUAGE, UploadProblem.UNKNOWN),
        ({"sports": []}, UploadField.SPORTS, UploadProblem.MISSING),
        ({"sports": ["gym", "chess"]}, UploadField.SPORTS, UploadProblem.UNKNOWN),
        ({"source_url": "ftp://example.org/x"}, UploadField.SOURCE_URL, UploadProblem.NOT_A_URL),
        ({"source_url": "https://"}, UploadField.SOURCE_URL, UploadProblem.NOT_A_URL),
        ({"source_url": "https://exa mple.org"}, UploadField.SOURCE_URL, UploadProblem.NOT_A_URL),
        ({"source_url": "http://[abc/x"}, UploadField.SOURCE_URL, UploadProblem.NOT_A_URL),
    ],
    ids=[
        "no-tier",
        "unknown-tier",
        "catalogue-tier",
        "other-without-note",
        "no-licence",
        "unknown-licence",
        "blank-attribution",
        "licence-url-not-a-link",
        "no-title",
        "no-type",
        "catalogue-type",
        "unknown-language",
        "no-sport",
        "unknown-sport",
        "ftp-url",
        "url-without-host",
        "url-with-space",
        "malformed-url",
    ],
)
def test_a_missing_or_unknown_answer_names_its_field(
    overrides: dict[str, str | list[str]], field: UploadField, problem: UploadProblem
) -> None:
    assert read_details(_answers(**overrides), known_sports=SPORTS) == {field: problem}


def test_every_problem_is_reported_at_once() -> None:
    problems = read_details(
        _answers(evidence_tier="", license_id="other", license_note=""), known_sports=SPORTS
    )
    assert problems == {
        UploadField.EVIDENCE_TIER: UploadProblem.MISSING,
        UploadField.LICENSE_NOTE: UploadProblem.MISSING,
    }


def test_answers_without_a_licence_field_are_refused_not_read_as_detect() -> None:
    # A sheet loaded before the select existed posts no `license_id` at all.
    fields = _answers().model_dump(exclude={"license_id"})
    assert read_details(UploadAnswers(**fields), known_sports=SPORTS) == {
        UploadField.LICENSE_ID: UploadProblem.MISSING
    }


def test_detect_reads_no_licence_and_ignores_the_licence_fields() -> None:
    template = read_details(
        _answers(license_id="detect", attribution="", license_url="not a link"),
        known_sports=SPORTS,
    )
    assert not isinstance(template, dict)
    assert template.licence is None


def test_a_picked_licence_is_the_admins() -> None:
    template = read_details(_answers(), known_sports=SPORTS)
    assert not isinstance(template, dict)
    assert template.licence is not None
    assert (template.licence.license_id, template.licence.license_source) == (
        LicenseId.CC_BY,
        LicenseSource.ADMIN,
    )


# --- The service ---------------------------------------------------------------------------


async def test_an_upload_puts_the_file_then_stores_the_source_as_uploaded() -> None:
    sources, storage = MemorySources(), MemoryStorage()
    data = pdf_bytes("store")

    outcome = await _service(sources, storage).upload(
        ADMIN, _answers(), File("deload-timing.pdf", data)
    )

    assert isinstance(outcome, SourceStored)
    source = outcome.source
    assert source.status is SourceStatus.UPLOADED
    assert source.storage_key == f"sources/{source.id}"
    assert storage.objects == {source.storage_key: data}
    assert storage.content_types[source.storage_key] == "application/pdf"
    assert source.content_hash == hashlib.sha256(data).hexdigest()
    assert (source.file_type, source.file_name, source.size_bytes) == (
        KnowledgeFileType.PDF,
        "deload-timing.pdf",
        len(data),
    )
    assert (source.uploaded_by_user_id, source.uploaded_at) == (ADMIN, NOW)
    assert list(sources.rows) == [source.id]


async def test_an_xml_upload_is_stored_as_a_jats_source() -> None:
    sources, storage = MemorySources(), MemoryStorage()

    outcome = await _service(sources, storage).upload(ADMIN, _answers(), File("paper.xml", _JATS))

    assert isinstance(outcome, SourceStored)
    source = outcome.source
    assert (source.status, source.file_type) == (SourceStatus.UPLOADED, KnowledgeFileType.JATS)
    assert storage.content_types[f"sources/{source.id}"] == "application/xml"


async def test_a_cc_by_upload_records_its_licence_and_attribution() -> None:
    outcome = await _service(storage=MemoryStorage()).upload(
        ADMIN, _answers(license_note="stray"), File("a.pdf", pdf_bytes("lic"))
    )

    assert isinstance(outcome, SourceStored)
    source = outcome.source
    assert source.status is SourceStatus.UPLOADED
    assert (source.license_id, source.license_source, source.license_note) == (
        LicenseId.CC_BY,
        LicenseSource.ADMIN,
        None,
    )
    assert (source.license_url, source.copyright_statement, source.attribution) == (
        "https://creativecommons.org/licenses/by/4.0/",
        "© 2024 The Authors",
        "M. Kowalska et al., Deload timing in resistance training, CC BY 4.0",
    )
    assert (source.pmcid, source.doi, source.pmid, source.isbn) == (None,) * 4


async def test_an_other_upload_records_the_permission_held() -> None:
    outcome = await _service(storage=MemoryStorage()).upload(
        ADMIN,
        _answers(license_id="other", license_note="  Owner's copy, scanned "),
        File("a.pdf", pdf_bytes("other")),
    )

    assert isinstance(outcome, SourceStored)
    assert (outcome.source.license_id, outcome.source.license_note) == (
        LicenseId.OTHER,
        "Owner's copy, scanned",
    )


async def test_a_detect_upload_waits_as_detecting_with_no_licence() -> None:
    outcome = await _service(storage=MemoryStorage()).upload(
        ADMIN, _answers(license_id="detect"), File("a.pdf", pdf_bytes("detect"))
    )

    assert isinstance(outcome, SourceStored)
    source = outcome.source
    assert source.status is SourceStatus.DETECTING
    assert (source.license_id, source.license_source, source.attribution) == (None,) * 3


async def test_a_refused_upload_stores_nothing_and_reports_file_and_field_problems() -> None:
    sources, storage = MemorySources(), MemoryStorage()

    outcome = await _service(sources, storage).upload(
        ADMIN, _answers(evidence_tier=""), File("notes.docx", b"PK\x03\x04")
    )

    assert outcome == UploadRefused(
        problems={
            UploadField.FILE: UploadProblem.UNLISTED_TYPE,
            UploadField.EVIDENCE_TIER: UploadProblem.MISSING,
        }
    )
    assert (sources.rows, storage.objects) == ({}, {})


async def test_a_file_over_the_limit_is_read_no_further_than_one_byte_past_it() -> None:
    class Counting(File):
        asked: ClassVar[list[int]] = []

        async def read(self, size: int = -1) -> bytes:
            self.asked.append(size)
            return await super().read(size)

    file = Counting("big.pdf", b"%PDF-" + b"x" * 5 * MAX_BYTES)
    outcome = await _service(storage=MemoryStorage()).upload(ADMIN, _answers(), file)

    assert outcome == UploadRefused(problems={UploadField.FILE: UploadProblem.TOO_LARGE})
    assert file.asked == [MAX_BYTES + 1]


async def test_no_file_at_all_is_a_missing_file() -> None:
    outcome = await _service(storage=MemoryStorage()).upload(ADMIN, _answers(), None)
    assert outcome == UploadRefused(problems={UploadField.FILE: UploadProblem.MISSING})


@pytest.mark.parametrize(
    "status",
    [SourceStatus.UPLOADED, SourceStatus.INGESTING, SourceStatus.INGESTED, SourceStatus.FAILED],
    ids=lambda s: s.value,
)
async def test_re_uploading_a_stored_file_changes_nothing_and_returns_that_source(
    status: SourceStatus,
) -> None:
    data = pdf_bytes(f"again-{status.value}")
    sources, storage = MemorySources(), MemoryStorage()
    existing = _stored(status, data, error="no_text" if status is SourceStatus.FAILED else None)
    sources.rows[existing.id] = existing

    # Blank details too: the file is what identifies a source (ADR-0011).
    outcome = await _service(sources, storage).upload(
        ADMIN, _answers(evidence_tier=""), File("renamed.pdf", data)
    )

    assert outcome == AlreadyStored(source=existing)
    assert sources.rows == {existing.id: existing}
    assert storage.objects == {}


async def test_an_upload_racing_another_of_the_same_file_removes_its_object() -> None:
    data = pdf_bytes("race")
    sources, storage = MemorySources(), MemoryStorage()
    sources.racing = _stored(SourceStatus.UPLOADED, data)

    outcome = await _service(sources, storage).upload(ADMIN, _answers(), File("race.pdf", data))

    assert outcome == AlreadyStored(source=sources.racing)
    assert storage.objects == {}


async def test_an_upload_racing_another_still_says_already_stored_when_cleanup_fails() -> None:
    data = pdf_bytes("race-cleanup")
    sources, storage = MemorySources(), MemoryStorage()
    sources.racing = _stored(SourceStatus.UPLOADED, data)
    storage.failing.add("delete")

    outcome = await _service(sources, storage).upload(ADMIN, _answers(), File("race.pdf", data))

    assert outcome == AlreadyStored(source=sources.racing)


async def test_without_storage_an_upload_is_refused_as_not_configured() -> None:
    sources = MemorySources()
    outcome = await _service(sources, None).upload(ADMIN, _answers(), File("a.pdf", pdf_bytes("n")))
    assert outcome == StorageUnavailable(reason="not_configured")
    assert sources.rows == {}


async def test_a_failed_put_stores_no_source() -> None:
    sources, storage = MemorySources(), MemoryStorage()
    storage.failing.add("put")

    outcome = await _service(sources, storage).upload(
        ADMIN, _answers(), File("a.pdf", pdf_bytes("put-fails"))
    )

    assert outcome == StorageUnavailable(reason="failed")
    assert sources.rows == {}


async def test_a_failed_insert_removes_the_object_it_put() -> None:
    class Broken(MemorySources):
        async def add(self, source: object) -> KnowledgeSource:
            raise RuntimeError("database down")

    storage = MemoryStorage()
    with pytest.raises(RuntimeError):
        await _service(Broken(), storage).upload(
            ADMIN, _answers(), File("a.pdf", pdf_bytes("insert-fails"))
        )
    assert storage.objects == {}


async def test_a_failed_insert_keeps_its_error_when_the_cleanup_fails_too() -> None:
    class Broken(MemorySources):
        async def add(self, source: object) -> KnowledgeSource:
            raise RuntimeError("database down")

    storage = MemoryStorage()
    storage.failing.add("delete")
    with pytest.raises(RuntimeError, match="database down"):
        await _service(Broken(), storage).upload(
            ADMIN, _answers(), File("a.pdf", pdf_bytes("insert-and-cleanup-fail"))
        )


async def test_delete_removes_the_object_and_then_the_source() -> None:
    data = pdf_bytes("delete")
    sources, storage = MemorySources(), MemoryStorage()
    source = _stored(SourceStatus.INGESTING, data)
    sources.rows[source.id] = source
    assert source.storage_key is not None
    storage.objects[source.storage_key] = data

    assert await _service(sources, storage).delete(source.id) == SourceDeleted()
    assert (sources.rows, storage.objects) == ({}, {})


async def test_a_failed_object_delete_keeps_the_source() -> None:
    sources, storage = MemorySources(), MemoryStorage()
    source = _stored(SourceStatus.INGESTED, pdf_bytes("keep"))
    sources.rows[source.id] = source
    storage.failing.add("delete")

    assert await _service(sources, storage).delete(source.id) == StorageUnavailable(reason="failed")
    assert sources.rows == {source.id: source}


async def test_delete_without_storage_keeps_a_source_that_has_a_file() -> None:
    sources = MemorySources()
    source = _stored(SourceStatus.UPLOADED, pdf_bytes("no-storage"))
    sources.rows[source.id] = source

    assert await _service(sources, None).delete(source.id) == StorageUnavailable(
        reason="not_configured"
    )
    assert source.id in sources.rows


async def test_delete_of_a_source_without_a_file_needs_no_storage() -> None:
    sources = MemorySources()
    source = _stored(SourceStatus.INGESTED, b"catalogue").model_copy(
        update={"storage_key": None, "source_type": SourceType.CATALOGUE}
    )
    sources.rows[source.id] = source

    assert await _service(sources, None).delete(source.id) == SourceDeleted()
    assert sources.rows == {}


async def test_delete_of_an_unknown_source_is_not_found() -> None:
    assert await _service(storage=MemoryStorage()).delete(uuid4()) == SourceNotFound()


async def test_a_source_gone_between_lookup_and_delete_is_not_found() -> None:
    class Vanishing(MemorySources):
        async def delete(self, source_id: UUID) -> bool:
            return False

    sources = Vanishing()
    source = _stored(SourceStatus.UPLOADED, pdf_bytes("vanish"))
    sources.rows[source.id] = source
    assert await _service(sources, MemoryStorage()).delete(source.id) == SourceNotFound()


@pytest.mark.parametrize(
    "status", [SourceStatus.INGESTED, SourceStatus.FAILED], ids=lambda s: s.value
)
async def test_re_ingest_puts_an_ingested_or_failed_source_back_in_the_queue(
    status: SourceStatus,
) -> None:
    sources = MemorySources()
    source = _stored(status, pdf_bytes(f"re-{status.value}"), error="no_text")
    sources.rows[source.id] = source
    other_admin = uuid4()

    outcome = await _service(sources).reingest(other_admin, source.id)

    assert isinstance(outcome, ReingestQueued)
    assert outcome.source.status is SourceStatus.UPLOADED
    assert (outcome.source.error, outcome.source.ingested_at) == (None, None)
    assert outcome.source.uploaded_by_user_id == other_admin


@pytest.mark.parametrize(
    "status", [SourceStatus.UPLOADED, SourceStatus.INGESTING], ids=lambda s: s.value
)
async def test_re_ingest_of_a_source_still_waiting_changes_nothing(status: SourceStatus) -> None:
    sources = MemorySources()
    source = _stored(status, pdf_bytes(f"wait-{status.value}"))
    sources.rows[source.id] = source

    assert await _service(sources).reingest(ADMIN, source.id) == NotReingestable(source=source)
    assert sources.rows[source.id] == source


async def test_re_ingest_of_an_unknown_source_is_not_found() -> None:
    assert await _service().reingest(ADMIN, uuid4()) == SourceNotFound()


async def test_re_ingest_racing_the_worker_reports_the_status_it_found() -> None:
    """The worker claimed the source between the lookup and the update."""

    class Racing(MemorySources):
        async def queue_reingest(
            self, source_id: UUID, *, requested_by: UUID
        ) -> KnowledgeSource | None:
            self.rows[source_id] = self.rows[source_id].model_copy(
                update={"status": SourceStatus.INGESTING}
            )
            return None

    sources = Racing()
    source = _stored(SourceStatus.FAILED, pdf_bytes("race-worker"), error="no_text")
    sources.rows[source.id] = source

    outcome = await _service(sources).reingest(ADMIN, source.id)

    assert outcome == NotReingestable(source=sources.rows[source.id])


async def test_re_ingest_racing_a_delete_is_not_found() -> None:
    class Racing(MemorySources):
        async def queue_reingest(
            self, source_id: UUID, *, requested_by: UUID
        ) -> KnowledgeSource | None:
            del self.rows[source_id]
            return None

    sources = Racing()
    source = _stored(SourceStatus.INGESTED, pdf_bytes("race-delete"))
    sources.rows[source.id] = source

    assert await _service(sources).reingest(ADMIN, source.id) == SourceNotFound()


async def test_list_returns_the_newest_first() -> None:
    sources = MemorySources()
    older = _stored(SourceStatus.INGESTED, pdf_bytes("old")).model_copy(
        update={"uploaded_at": datetime(2026, 11, 1, tzinfo=UTC)}
    )
    newer = _stored(SourceStatus.UPLOADED, pdf_bytes("new"))
    sources.rows = {older.id: older, newer.id: newer}

    assert await _service(sources).list() == [newer, older]
