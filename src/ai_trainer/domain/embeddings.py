"""What an embedding call returns (ADR-0007, ADR-0033): vectors, a rate limit to wait out, or a
failure. A gateway never raises for a model or provider failure."""

from dataclasses import dataclass
from enum import StrEnum


class QueryTask(StrEnum):
    """What a search query is, which picks its instruction line (ADR-0033, #429): a question
    about training, or an athlete's description of a gym movement."""

    QUESTION = "question"
    EXERCISE = "exercise"


@dataclass(frozen=True, slots=True)
class Embedded:
    """One vector per input, in input order, and the model the response names."""

    vectors: tuple[tuple[float, ...], ...]
    model: str


@dataclass(frozen=True, slots=True)
class RateLimited:
    """OpenRouter answered 429. `retry_after_seconds` is set only when it said how long."""

    retry_after_seconds: float | None = None


class EmbeddingFailure(StrEnum):
    TIMEOUT = "timeout"
    ERROR = "error"
    # A vector of another length than `EMBEDDING_DIM`, or not one per input (ADR-0033).
    WRONG_SHAPE = "wrong_shape"


@dataclass(frozen=True, slots=True)
class EmbeddingFailed:
    reason: EmbeddingFailure


type EmbeddingOutcome = Embedded | RateLimited | EmbeddingFailed
