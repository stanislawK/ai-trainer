"""The band-order question's answer (PRD-0010 B39, ADR-0015, ticket #431): a tap on a place ranks
the band and moves the heavier ones up, a tap that narrows the places rewrites the open question
to that half, and the next band that needs a place is asked once this one is settled."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_trainer.application.ports.band_order import BandOrderPort
from ai_trainer.application.reply.shared import ReplyServices
from ai_trainer.domain.clarification import (
    BAND_PURPOSE,
    ClarificationNotFoundError,
    StoredClarification,
    UnknownOptionError,
)
from ai_trainer.domain.sports.gym.bands import NARROW_PREFIX, band_question, narrowed, place_of


@dataclass(frozen=True, slots=True)
class BandPlaced:
    """A band question as answered: closed once the band has its rank, or still open and
    narrowed to fewer places. `next` is the question about the next band that needs a place."""

    clarification: StoredClarification
    next: StoredClarification | None = None


def _scale(question: StoredClarification, services: ReplyServices) -> BandOrderPort:
    sport = question.clarification.sport
    order = None if sport is None else services.band_orders.get(sport)
    if order is None or question.clarification.subject is None:
        raise ClarificationNotFoundError(question.id)
    return order


async def answer_band(
    user_id: UUID,
    question: StoredClarification,
    option_ids: Sequence[str],
    *,
    services: ReplyServices,
) -> BandPlaced:
    """The tap on an open band question. The option ID is trusted, label text never is: an ID the
    question did not offer, or not exactly one, is an `UnknownOptionError` and nothing changes.
    The question closes first, with one conditional UPDATE, so two taps at once rank the band
    once. Raises `ClarificationNotFoundError` for a sport with no band scale."""
    order = _scale(question, services)
    document = question.clarification
    if len(option_ids) != 1:
        raise UnknownOptionError(option_ids)
    picked = option_ids[0]
    if picked.startswith(NARROW_PREFIX):
        revised = narrowed(
            document,
            picked,
            await order.ranked(user_id),
            max_options=services.max_clarification_options,
        )
        return BandPlaced(await services.clarifications.revise(user_id, question.id, revised))
    rank = place_of(document, picked)
    resolved = await services.clarifications.resolve(
        user_id, question.id, "answered", option_ids=[picked], resolved_at=services.clock.now()
    )
    try:
        await order.place(user_id, document.subject or "", rank)
    except BaseException:
        # The band was not ranked, so the card goes back to be tapped again.
        await asyncio.shield(services.clarifications.reopen(user_id, question.id))
        raise
    return BandPlaced(resolved, await _ask_next(user_id, question, order, services))


async def _ask_next(
    user_id: UUID, answered: StoredClarification, order: BandOrderPort, services: ReplyServices
) -> StoredClarification | None:
    """The question about the next band with no rank that was never asked about, one at a time
    (ADR-0015 invariant 1), under the same log as the one just answered."""
    label = await order.next_unplaced(user_id)
    ranked = await order.ranked(user_id)
    if label is None or not ranked:
        return None
    return await services.clarifications.add(
        user_id,
        answered.conversation_id,
        answered.message_id,
        BAND_PURPOSE,
        band_question(label, ranked, max_options=services.max_clarification_options),
        created_at=services.clock.now(),
    )
