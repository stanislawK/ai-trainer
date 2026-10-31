from collections.abc import Collection, Mapping
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.clarification import Clarification


class DraftSettlerPort(Protocol):
    """What a sport settles on a draft before it is confirmed, mapped by `sport_id` in the
    composition root, so core code never branches on a sport (ADR-0006). The gym's settles
    exercise names (B37, ADR-0031). It makes no model call: it runs on the logging hot path (G14).
    Every method is scoped to one `user_id` from the authenticated session."""

    async def settle(self, user_id: UUID, payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """The payload with what the athlete's history already knows filled in."""
        ...

    async def next_question(
        self, user_id: UUID, payload: Mapping[str, JsonValue], *, settled: Collection[str] = ()
    ) -> Clarification | None:
        """The question about the first name still open, leaving out `settled`, those already
        asked about; `None` when nothing is left to ask."""
        ...

    def answered(
        self, payload: Mapping[str, JsonValue], subject: str, option_id: str
    ) -> dict[str, JsonValue]:
        """The payload as the option the athlete tapped on the question about `subject` reads
        it. The caller has checked that the question offered it (ADR-0015 invariant 4)."""
        ...

    def mentions(self, payload: Mapping[str, JsonValue], subject: str) -> bool:
        """Whether the draft still carries what a question asks about, after an edit."""
        ...
