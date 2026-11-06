"""Knowledge sources, the manifest of the knowledge base (ADR-0011, ADR-0033, ADR-0036, PRD B7).

An admin uploads a file with its details; the file goes to the private `uploads` bucket and the
source is stored as `uploaded` for the ingestion worker. A source is identified by the SHA-256
of its bytes, so the same file uploaded twice is one source. A staged source waits as `proposed`
until an admin approves it, and carries a structured licence (ADR-0036).
"""

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


class SourceType(StrEnum):
    RESEARCH_PAPER = "research_paper"
    BOOK = "book"
    TALK_OR_PODCAST = "talk_or_podcast"
    ARTICLE = "article"
    # The exercise catalogue's texts (ADR-0033, #427); never uploaded.
    CATALOGUE = "catalogue"


class EvidenceTier(StrEnum):
    """The kind of source, ranked, not the certainty of any claim in it (ADR-0033)."""

    PEER_REVIEWED = "peer_reviewed"
    COACHING_BOOK = "coaching_book"
    ARTICLE_OR_TRANSCRIPT = "article_or_transcript"
    # Reserved for the catalogue's rows (#427); never offered on the form.
    CATALOGUE = "catalogue"


class SourceLanguage(StrEnum):
    EN = "en"
    PL = "pl"


class SourceStatus(StrEnum):
    # A form upload whose licence the `ingest` container reads from the file first; it then
    # becomes `proposed` (the #454 amendment). The worker never ingests it.
    DETECTING = "detecting"
    # Staged by a token or an agent; the worker never reads it until an admin approves it.
    PROPOSED = "proposed"
    REJECTED = "rejected"
    UPLOADED = "uploaded"
    INGESTING = "ingesting"
    INGESTED = "ingested"
    FAILED = "failed"


class KnowledgeFileType(StrEnum):
    PDF = "pdf"
    EPUB = "epub"
    MARKDOWN = "markdown"
    TEXT = "text"
    VTT = "vtt"
    SRT = "srt"
    # A PMC article's XML: fetched by its PMC ID once approved, or uploaded as `.xml` (ADR-0036).
    JATS = "jats"


class LicenseId(StrEnum):
    """ADR-0036's closed set. Every CC licence is admitted, the app being non-commercial; `other`
    needs a note saying what permission the admin holds."""

    CC0 = "cc0"
    CC_BY = "cc-by"
    CC_BY_SA = "cc-by-sa"
    CC_BY_NC = "cc-by-nc"
    CC_BY_NC_SA = "cc-by-nc-sa"
    CC_BY_ND = "cc-by-nd"
    CC_BY_NC_ND = "cc-by-nc-nd"
    OTHER = "other"


class LicenseSource(StrEnum):
    """Where a source's licence was read."""

    PMC = "pmc"
    JATS = "jats"
    ADMIN = "admin"
    # Detected in the file's front matter (the #454 amendment).
    FILE = "file"


class LicenceFlag(StrEnum):
    """Licences flagged in the list until a lawyer has read their terms (ADR-0036)."""

    SHARE_ALIKE = "sa"
    NO_DERIVATIVES = "nd"


_FLAGS: Mapping[LicenseId, LicenceFlag] = {
    LicenseId.CC_BY_SA: LicenceFlag.SHARE_ALIKE,
    LicenseId.CC_BY_NC_SA: LicenceFlag.SHARE_ALIKE,
    LicenseId.CC_BY_ND: LicenceFlag.NO_DERIVATIVES,
    LicenseId.CC_BY_NC_ND: LicenceFlag.NO_DERIVATIVES,
}
# Statuses that may wait without a licence: one is still being read from the file, or an admin
# sets it before approving (the #454 amendment; the table's `licence_set` CHECK).
UNLICENSED_STATUSES = frozenset({SourceStatus.DETECTING, SourceStatus.PROPOSED})


def licence_flag(license_id: LicenseId | None) -> LicenceFlag | None:
    return None if license_id is None else _FLAGS.get(license_id)


# What the upload form offers, in the order it offers them.
UPLOAD_SOURCE_TYPES = (
    SourceType.RESEARCH_PAPER,
    SourceType.BOOK,
    SourceType.TALK_OR_PODCAST,
    SourceType.ARTICLE,
)
UPLOAD_EVIDENCE_TIERS = (
    EvidenceTier.PEER_REVIEWED,
    EvidenceTier.COACHING_BOOK,
    EvidenceTier.ARTICLE_OR_TRANSCRIPT,
)
REINGESTABLE = frozenset({SourceStatus.INGESTED, SourceStatus.FAILED})


class ProposalDecision(StrEnum):
    """What an admin does with selected proposals on `/admin/knowledge` (ADR-0036, #461)."""

    APPROVE = "approve"
    REJECT = "reject"

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.UPLOADED if self is ProposalDecision.APPROVE else SourceStatus.REJECTED


class SourceFilter(StrEnum):
    """The list's filter chips on `/admin/knowledge`, in their order (#461). Statuses without a
    chip of their own show under All."""

    ALL = "all"
    PROPOSED = "proposed"
    INGESTED = "ingested"
    REJECTED = "rejected"

    @classmethod
    def read(cls, value: str | None) -> SourceFilter:
        """An unknown or missing value is All."""
        return next((chip for chip in cls if chip.value == value), cls.ALL)

    def shows(self, status: SourceStatus) -> bool:
        return self is SourceFilter.ALL or self.value == status.value


class SkipReason(StrEnum):
    """Why a selected proposal was left as it was."""

    # No longer `proposed`: decided in another tab since the page was drawn.
    MOVED = "moved"
    # Still `proposed`, with no licence: it can't leave `proposed` (ADR-0036 invariant 4).
    UNLICENSED = "unlicensed"
    # Deleted since the page was drawn.
    GONE = "gone"


_EXTENSIONS: Mapping[str, KnowledgeFileType] = {
    ".pdf": KnowledgeFileType.PDF,
    ".epub": KnowledgeFileType.EPUB,
    ".md": KnowledgeFileType.MARKDOWN,
    ".markdown": KnowledgeFileType.MARKDOWN,
    ".txt": KnowledgeFileType.TEXT,
    ".vtt": KnowledgeFileType.VTT,
    ".srt": KnowledgeFileType.SRT,
    ".xml": KnowledgeFileType.JATS,
}
CONTENT_TYPES: Mapping[KnowledgeFileType, str] = {
    KnowledgeFileType.PDF: "application/pdf",
    KnowledgeFileType.EPUB: "application/epub+zip",
    KnowledgeFileType.MARKDOWN: "text/markdown; charset=utf-8",
    KnowledgeFileType.TEXT: "text/plain; charset=utf-8",
    KnowledgeFileType.VTT: "text/vtt; charset=utf-8",
    KnowledgeFileType.SRT: "application/x-subrip",
    KnowledgeFileType.JATS: "application/xml",
}
_JATS_ROOT = re.compile(r"<article[\s>/]")
_JATS_WINDOW = 16 * 1024
# EPUB OCF: the archive's first entry is `mimetype`, stored uncompressed with no extra field, so
# its name and value sit at fixed offsets in the first local file header.
_EPUB_MIMETYPE = b"PK\x03\x04", b"mimetype", b"application/epub+zip"


class UploadProblem(StrEnum):
    MISSING = "missing"
    UNKNOWN = "unknown"
    TOO_LARGE = "too_large"
    UNLISTED_TYPE = "unlisted_type"
    NOT_A_URL = "not_a_url"


class DuplicateSourceError(Exception):
    """A source with the same content hash is already stored."""


def _lowercase(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


# The bibliographic identifiers in the formats the table's CHECKs hold, refused here first
# (#453), and shared by the source template (#456).
type PmcId = Annotated[str, Field(pattern=r"^PMC[0-9]+$")]
# A bare DOI, lowercased: DOIs are case-insensitive, so any spelling is one source.
type Doi = Annotated[str, BeforeValidator(_lowercase), Field(pattern=r"^10\.[0-9.]+/.")]
type PubMedId = Annotated[str, Field(pattern=r"^[0-9]+$")]


class SourceDetails(BaseModel):
    """What every source records about the work itself, whichever way it came in."""

    model_config = ConfigDict(frozen=True)

    title: str
    author: str | None
    source_type: SourceType
    evidence_tier: EvidenceTier
    language: SourceLanguage
    sports: tuple[str, ...]
    topics: tuple[str, ...]
    # What permission the admin holds: required for `other`, and only there (ADR-0036).
    license_note: str | None
    source_url: str | None
    pmcid: PmcId | None = None
    doi: Doi | None = None
    pmid: PubMedId | None = None
    isbn: str | None = None


class NewKnowledgeSource(SourceDetails):
    id: UUID
    # Both None while the licence is detected or awaits an admin (UNLICENSED_STATUSES).
    license_id: LicenseId | None = None
    license_source: LicenseSource | None = None
    license_url: str | None = None
    # The source's copyright statement, verbatim.
    copyright_statement: str | None = None
    attribution: str | None = None
    # What detection found in the file's front matter: the matched line and where it was, or
    # why no licence was set (the #454 amendment).
    license_matched_line: str | None = None
    license_locator: str | None = None
    license_detection_reason: str | None = None
    content_hash: str
    # None for the catalogue's rows, which have no uploaded file (#427).
    file_type: KnowledgeFileType | None
    file_name: str | None
    size_bytes: int | None
    storage_key: str | None
    status: SourceStatus = SourceStatus.UPLOADED
    # The admin who uploaded it or last asked for re-ingest: the acting user of its embedding
    # calls (ADR-0018). None once that account is deleted.
    uploaded_by_user_id: UUID | None
    uploaded_at: datetime

    @model_validator(mode="after")
    def _licensed_unless_waiting(self) -> Self:
        # The table's `licence_pair`, `licence_set` and `other_license_note` CHECKs, refused
        # here first.
        if (self.license_id is None) != (self.license_source is None):
            raise ValueError("a licence and its source are set together")
        if self.license_id is None and self.status not in UNLICENSED_STATUSES:
            raise ValueError(f"a {self.status} source needs a licence")
        if self.license_id is LicenseId.OTHER and self.license_note is None:
            raise ValueError("an `other` licence needs a note saying what permission is held")
        return self


class KnowledgeSource(NewKnowledgeSource):
    # One of `IngestFailure`'s values for a `failed` source (#423).
    error: str | None = None
    ingested_at: datetime | None = None
    # What the source's chunks hold, for the admin list (#423): 0 and None until ingested.
    chunk_count: int = 0
    embedding_model: str | None = None

    @property
    def licence_flag(self) -> LicenceFlag | None:
        return licence_flag(self.license_id)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def _content_matches(file_type: KnowledgeFileType, data: bytes) -> bool:
    match file_type:
        case KnowledgeFileType.PDF:
            return data.startswith(b"%PDF-")
        case KnowledgeFileType.EPUB:
            signature, name, value = _EPUB_MIMETYPE
            return data[:4] == signature and data[30:38] == name and data[38:58] == value
        case KnowledgeFileType.VTT:
            text = _text(data)
            return text is not None and text.startswith("WEBVTT")
        case KnowledgeFileType.MARKDOWN | KnowledgeFileType.TEXT | KnowledgeFileType.SRT:
            return _text(data) is not None
        case KnowledgeFileType.JATS:
            # A JATS article opens with `<article>` (or an `<article>` inside a set), after at
            # most an XML declaration and a DOCTYPE; any other XML is not one.
            text = _text(data)
            return text is not None and _JATS_ROOT.search(text[:_JATS_WINDOW]) is not None


def file_type_for(file_name: str) -> KnowledgeFileType | None:
    """The type a file's extension names, before its bytes are seen; None for an unlisted one."""
    return _EXTENSIONS.get(PurePosixPath(file_name).suffix.lower())


def check_file(
    file_name: str | None, data: bytes, *, max_bytes: int
) -> KnowledgeFileType | UploadProblem:
    """The file's type, or why it is refused. `data` may stop one byte past `max_bytes`."""
    if not file_name or not data:
        return UploadProblem.MISSING
    if len(data) > max_bytes:
        return UploadProblem.TOO_LARGE
    file_type = file_type_for(file_name)
    if file_type is None or not _content_matches(file_type, data):
        return UploadProblem.UNLISTED_TYPE
    return file_type


def web_url(value: str) -> str | UploadProblem:
    """An http(s) link with a host and no spaces, or why it isn't one."""
    try:
        parts = urlsplit(value)
    except ValueError:  # e.g. an unclosed IPv6 bracket
        return UploadProblem.NOT_A_URL
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or any(c.isspace() for c in value)
    ):
        return UploadProblem.NOT_A_URL
    return value
