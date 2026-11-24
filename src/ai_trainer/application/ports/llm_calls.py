from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.llm_calls import LlmCall, NewLlmCall


class LlmCallsRepositoryPort(Protocol):
    """Persists `llm_calls` rows (ADR-0018). Every method is user-scoped (ADR-0004 invariant 2)."""

    async def record(self, call: NewLlmCall) -> LlmCall: ...

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]: ...


class PictureDaySpendPort(Protocol):
    """The card picture path's spend across every athlete, for `IMAGE_GENERATION_DAILY_CAP_USD`
    (ADR-0031 *Picture on the card*, #643). It returns one sum and no user data."""

    async def spent_between(
        self, start: datetime, end: datetime, *, picture_price: Decimal
    ) -> Decimal:
        """The cost of every `media.picture_brief`, `picture_brief_check` and `media.picture` row
        created in `[start, end)`, whoever wrote it; a picture row with no cost counts at
        `picture_price`, any other row with no cost at zero."""
        ...
