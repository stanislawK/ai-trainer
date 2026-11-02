"""Knowledge sources, the manifest of the knowledge base (ADR-0011, ADR-0033, PRD B7).

An admin uploads a file with its details; the file goes to the private `uploads` bucket and the
source is stored as `uploaded` for the ingestion worker. A source is identified by the SHA-256
of its bytes, so the same file uploaded twice is one source.
"""

import hashlib
from collections.abc import Collection, Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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

_EXTENSIONS: Mapping[str, KnowledgeFileType] = {
    ".pdf": KnowledgeFileType.PDF,
    ".epub": KnowledgeFileType.EPUB,
    ".md": KnowledgeFileType.MARKDOWN,
    ".markdown": KnowledgeFileType.MARKDOWN,
    ".txt": KnowledgeFileType.TEXT,
    ".vtt": KnowledgeFileType.VTT,
    ".srt": KnowledgeFileType.SRT,
}
CONTENT_TYPES: Mapping[KnowledgeFileType, str] = {
    KnowledgeFileType.PDF: "application/pdf",
    KnowledgeFileType.EPUB: "application/epub+zip",
    KnowledgeFileType.MARKDOWN: "text/markdown; charset=utf-8",
    KnowledgeFileType.TEXT: "text/plain; charset=utf-8",
    KnowledgeFileType.VTT: "text/vtt; charset=utf-8",
    KnowledgeFileType.SRT: "application/x-subrip",
}
# EPUB OCF: the archive's first entry is `mimetype`, stored uncompressed with no extra field, so
# its name and value sit at fixed offsets in the first local file header.
_EPUB_MIMETYPE = b"PK\x03\x04", b"mimetype", b"application/epub+zip"


class UploadField(StrEnum):
    FILE = "file"
    TITLE = "title"
    SOURCE_TYPE = "source_type"
    EVIDENCE_TIER = "evidence_tier"
    LANGUAGE = "language"
    LICENSE_NOTE = "license_note"
    SOURCE_URL = "source_url"
    SPORTS = "sports"


class UploadProblem(StrEnum):
    MISSING = "missing"
    UNKNOWN = "unknown"
    TOO_LARGE = "too_large"
    UNLISTED_TYPE = "unlisted_type"
    NOT_A_URL = "not_a_url"


class DuplicateSourceError(Exception):
    """A source with the same content hash is already stored."""


class UploadAnswers(BaseModel):
    """The form's fields as typed, kept so a refused form shows them again."""

    model_config = ConfigDict(frozen=True)

    title: str = ""
    author: str = ""
    source_type: str = ""
    evidence_tier: str = ""
    language: str = ""
    topics: str = ""
    license_note: str = ""
    source_url: str = ""
    sports: tuple[str, ...] = ()


class SourceDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    author: str | None
    source_type: SourceType
    evidence_tier: EvidenceTier
    language: SourceLanguage
    sports: tuple[str, ...]
    topics: tuple[str, ...]
    license_note: str
    source_url: str | None


class NewKnowledgeSource(SourceDetails):
    id: UUID
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


class KnowledgeSource(NewKnowledgeSource):
    error: str | None = None
    ingested_at: datetime | None = None


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


def check_file(
    file_name: str | None, data: bytes, *, max_bytes: int
) -> KnowledgeFileType | UploadProblem:
    """The file's type, or why it is refused. `data` may stop one byte past `max_bytes`."""
    if not file_name or not data:
        return UploadProblem.MISSING
    if len(data) > max_bytes:
        return UploadProblem.TOO_LARGE
    file_type = _EXTENSIONS.get(PurePosixPath(file_name).suffix.lower())
    if file_type is None or not _content_matches(file_type, data):
        return UploadProblem.UNLISTED_TYPE
    return file_type


def _choice[T: StrEnum](value: str, offered: Collection[T], kind: type[T]) -> T | UploadProblem:
    if not value.strip():
        return UploadProblem.MISSING
    try:
        choice = kind(value.strip())
    except ValueError:
        return UploadProblem.UNKNOWN
    return choice if choice in offered else UploadProblem.UNKNOWN


def _url(value: str) -> str | UploadProblem | None:
    value = value.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or any(c.isspace() for c in value)
    ):
        return UploadProblem.NOT_A_URL
    return value


def _sports(values: tuple[str, ...], known: Collection[str]) -> tuple[str, ...] | UploadProblem:
    if not values:
        return UploadProblem.MISSING
    if any(value not in known for value in values):
        return UploadProblem.UNKNOWN
    return tuple(dict.fromkeys(values))


def _topics(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t.strip().lower() for t in value.split(",") if t.strip()))


def read_details(
    answers: UploadAnswers, *, known_sports: Collection[str]
) -> SourceDetails | dict[UploadField, UploadProblem]:
    """Every problem at once, so the form marks each field in one round trip."""
    source_type = _choice(answers.source_type, UPLOAD_SOURCE_TYPES, SourceType)
    tier = _choice(answers.evidence_tier, UPLOAD_EVIDENCE_TIERS, EvidenceTier)
    language = _choice(answers.language, tuple(SourceLanguage), SourceLanguage)
    url = _url(answers.source_url)
    sports = _sports(answers.sports, known_sports)
    title, license_note = answers.title.strip(), answers.license_note.strip()
    checked: dict[UploadField, object] = {
        UploadField.TITLE: title or UploadProblem.MISSING,
        UploadField.SOURCE_TYPE: source_type,
        UploadField.EVIDENCE_TIER: tier,
        UploadField.LANGUAGE: language,
        UploadField.LICENSE_NOTE: license_note or UploadProblem.MISSING,
        UploadField.SOURCE_URL: url,
        UploadField.SPORTS: sports,
    }
    problems = {f: v for f, v in checked.items() if isinstance(v, UploadProblem)}
    if problems:
        return problems
    assert not isinstance(source_type, UploadProblem)
    assert not isinstance(tier, UploadProblem)
    assert not isinstance(language, UploadProblem)
    assert not isinstance(url, UploadProblem)
    assert not isinstance(sports, UploadProblem)
    return SourceDetails(
        title=title,
        author=answers.author.strip() or None,
        source_type=source_type,
        evidence_tier=tier,
        language=language,
        sports=sports,
        topics=_topics(answers.topics),
        license_note=license_note,
        source_url=url,
    )
