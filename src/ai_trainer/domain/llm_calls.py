from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class LlmCallOutcome(StrEnum):
    """Every gateway call writes exactly one row with one of these (ADR-0018 invariant 1)."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"


class NewLlmCall(BaseModel):
    """A call about to be recorded; the repository assigns `id` and `created_at`."""

    user_id: UUID
    template_id: str
    template_version: int
    model: str
    input_tokens: int
    output_tokens: int
    cost: Decimal | None
    latency_ms: int
    outcome: LlmCallOutcome


class LlmCall(NewLlmCall):
    """A persisted `llm_calls` row."""

    id: UUID
    created_at: datetime
