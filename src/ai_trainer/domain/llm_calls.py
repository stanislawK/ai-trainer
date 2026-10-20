from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LlmCallOutcome(StrEnum):
    """Every gateway call writes exactly one row with one of these (ADR-0018 invariant 1)."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELLED = "cancelled"


# OpenRouter's `reasoning.effort` levels (ADR-0026 step 3). A model that lacks the one asked
# for gets the nearest level it supports; where reasoning is mandatory, `none` is ignored.
type ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

type LatencyPercentile = Literal["p50", "p75", "p90", "p99"]


class ProviderPreferences(BaseModel):
    """How OpenRouter picks an upstream for one template (ADR-0026 step 4). Fallbacks stay
    on. There is no `order`: an explicit order turns off OpenRouter's latency-aware load
    balancing, so it is refused rather than ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sort: Literal["price", "throughput", "latency"] | None = None
    # Seconds. A number is read as p50; endpoints over it move to the back, not out.
    preferred_max_latency: (
        Annotated[float, Field(gt=0)]
        | dict[LatencyPercentile, Annotated[float, Field(gt=0)]]
        | None
    ) = None
    quantizations: list[str] | None = None
    only: list[str] | None = None


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
