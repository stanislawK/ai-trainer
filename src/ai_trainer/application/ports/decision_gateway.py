from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.domain.llm_calls import LlmCallOutcome


@dataclass(frozen=True, slots=True)
class DecisionResult[OutputT]:
    """Never raises for a model failure: `output` is `None` unless `outcome` is success.

    `probabilities` holds each choice's distribution keyed by option name, and each score's
    keyed by level; a probability question's P(yes) is its value in `output` (ADR-0020)."""

    output: OutputT | None
    outcome: LlmCallOutcome
    probabilities: dict[str, dict[str, float]] = field(default_factory=dict)


class DecisionGatewayPort(Protocol):
    """The only way the application asks Jev to classify, route, gate or score (ADR-0020)."""

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]: ...
