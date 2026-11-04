"""What the ingestion worker hands its repository, and what it reports per source (ADR-0036,
#423). A failed source's `error` holds one `IngestFailure` value; the admin page words it."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from ai_trainer.domain.knowledge_document import NewKnowledgeChunk


class IngestFailure(StrEnum):
    # The file's type has no reader yet (EPUB/VTT/SRT #426, JATS #463).
    NO_READER = "no_reader"
    NOT_UTF8 = "not_utf8"
    # Nothing to embed: an empty file, or only headings or references.
    NO_TEXT = "no_text"
    NO_TEXT_LAYER = "no_text_layer"
    ENCRYPTED_FILE = "encrypted_file"
    CORRUPT_FILE = "corrupt_file"
    FILE_MISSING = "file_missing"
    STORAGE_FAILED = "storage_failed"
    EMBEDDING_FAILED = "embedding_failed"
    EMBEDDING_TIMEOUT = "embedding_timeout"
    # OpenRouter kept answering 429 past `KNOWLEDGE_EMBEDDING_MAX_ATTEMPTS`.
    RATE_LIMITED = "rate_limited"
    # A vector of the wrong length, or not one per input (ADR-0033).
    WRONG_SHAPE = "wrong_shape"
    # The uploading admin's account is gone, so no one can be the calls' acting user (ADR-0018);
    # a re-ingest names an admin again.
    NO_ACTING_ADMIN = "no_acting_admin"
    # Anything else that went wrong reading, chunking or embedding the file: a bug or a file no
    # reader expected. The source fails instead of crashing the worker on every retry.
    INGEST_ERROR = "ingest_error"


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    chunk: NewKnowledgeChunk
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class IngestReport:
    """One source's outcome. `gone` is a source deleted, or taken over, while it was read."""

    source_id: UUID
    result: Literal["ingested", "failed", "gone"]
    chunks: int = 0
    reason: IngestFailure | None = None
