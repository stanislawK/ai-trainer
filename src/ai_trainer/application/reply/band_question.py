"""The band-order question's answer (PRD-0010 B39, ADR-0015, ticket #431): a tap on a place ranks
the band and moves the heavier ones up, a tap that narrows the places rewrites the open question
to that half, and the next band that needs a place is asked once this one is settled."""

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
from ai_trainer.domain.sports.gym.bands import (
    NARROW_PREFIX,
    band_question,
    narrowed,
    place_of,
    places_of,
)


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
    A card whose ranking changed since it was asked (another card answered meanwhile) is asked
    again from the athlete's bands as they are, and nothing is ranked from the stale tap. The band
    is ranked first and the question closed second, with one conditional UPDATE: ranking an
    already ranked band is a no-op, so two taps at once rank it once, and a failure leaves the
    card to tap again. Raises `ClarificationNotFoundError` for a sport with no band scale."""
    order = _scale(question, services)
    document = question.clarification
    if len(option_ids) != 1 or option_ids[0] not in {option.id for option in document.options}:
        raise UnknownOptionError(option_ids)
    picked = option_ids[0]
    ranked = await order.ranked(user_id)
    if places_of(document) != len(ranked) + 1:
        fresh = band_question(
            document.subject or "", ranked, max_options=services.max_clarification_options
        )
        return BandPlaced(await services.clarifications.revise(user_id, question.id, fresh))
    if picked.startswith(NARROW_PREFIX):
        revised = narrowed(document, picked, ranked, max_options=services.max_clarification_options)
        return BandPlaced(await services.clarifications.revise(user_id, question.id, revised))
    await order.place(user_id, document.subject or "", place_of(document, picked))
    resolved = await services.clarifications.resolve(
        user_id, question.id, "answered", option_ids=[picked], resolved_at=services.clock.now()
    )
    return BandPlaced(resolved, await _ask_next(user_id, question, order, services))


async def skip_band(
    user_id: UUID, question: StoredClarification, *, services: ReplyServices
) -> BandPlaced:
    """ "Skip for now": the band keeps `rank` NULL and shows by its label, and the next band that
    needs a place is asked, one at a time (ADR-0015). Raises `ClarificationNotFoundError` for a
    sport with no band scale and `ClarificationResolvedError` for a closed question."""
    order = _scale(question, services)
    resolved = await services.clarifications.resolve(
        user_id, question.id, "abandoned", option_ids=None, resolved_at=services.clock.now()
    )
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
