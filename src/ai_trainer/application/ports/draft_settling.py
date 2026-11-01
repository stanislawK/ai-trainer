from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.clarification import Clarification
from ai_trainer.domain.exercises import ExerciseLabel


@dataclass(frozen=True, slots=True)
class DecisionRef:
    """A Jev decision a draft carries, and the draft fields it filled: what a
    `decision_overridden` event names (ADR-0034), never a value."""

    decision_id: str
    version: int
    fields: tuple[str, ...]


class DraftSettlerPort(Protocol):
    """What a sport settles on a draft before it is confirmed, mapped by `sport_id` in the
    composition root, so core code never branches on a sport (ADR-0006). The gym's settles
    exercise names (B37, ADR-0031). It runs on the logging hot path (G14): history is a database
    read, and a name history doesn't know costs at most one Jev decision call, bounded by its
    timeout. Every method is scoped to one `user_id` from the authenticated session."""

    async def settle(
        self, user_id: UUID, payload: Mapping[str, JsonValue], *, pick: bool = True
    ) -> dict[str, JsonValue]:
        """The payload with what the athlete's history already knows filled in, then, unless
        `pick` is off, what Jev confidently picks for a name history doesn't know. The athlete
        confirms it all; nothing is stored here."""
        ...

    def overridden_picks(
        self, before: Mapping[str, JsonValue], after: Mapping[str, JsonValue]
    ) -> Sequence[DecisionRef]:
        """The Jev decisions whose pick an edit from `before` to `after` undid."""
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

    def typed(
        self, payload: Mapping[str, JsonValue], subject: str, name: str
    ) -> dict[str, JsonValue]:
        """The payload as the card's name field reads it: `subject` becomes `name`, kept like the
        athlete's own name (ticket #402). The caller has checked that `name` can be stored."""
        ...

    async def labels(
        self, user_id: UUID, payload: Mapping[str, JsonValue], *, answered: Collection[str] = ()
    ) -> Mapping[str, ExerciseLabel]:
        """How the draft's card names each settled entry, by its name as typed. `answered` are
        the names the athlete answered about on this draft, which no near match explains."""
        ...

    def mentions(self, payload: Mapping[str, JsonValue], subject: str) -> bool:
        """Whether the draft still carries what a question asks about, after an edit."""
        ...

    def names(self, payload: Mapping[str, JsonValue]) -> frozenset[str]:
        """What the draft's questions could ask about, each once, as `next_question`'s `settled`
        compares them."""
        ...
