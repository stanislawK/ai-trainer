from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.domain.llm_calls import LlmCallOutcome


@dataclass(frozen=True, slots=True)
class LlmGatewayResult[OutputT: BaseModel]:
    """Never raises for a model/provider failure: `output` or `friendly_error` is set (ADR-0007)."""

    output: OutputT | None
    friendly_error: str | None
    outcome: LlmCallOutcome


class LlmGatewayPort(Protocol):
    """The only way the application layer reaches a model; Pydantic AI stays in `llm/` (ADR-0007)"""

    async def run[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        output_type: type[OutputT],
        instructions: str,
        prompt: str,
    ) -> LlmGatewayResult[OutputT]: ...
