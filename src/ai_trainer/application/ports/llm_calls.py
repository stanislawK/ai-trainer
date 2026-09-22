from typing import Protocol
from uuid import UUID

from ai_trainer.domain.llm_calls import LlmCall, NewLlmCall


class LlmCallsRepositoryPort(Protocol):
    """Persists `llm_calls` rows (ADR-0018). Every method is user-scoped (ADR-0004 invariant 2)."""

    async def record(self, call: NewLlmCall) -> LlmCall: ...

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]: ...
