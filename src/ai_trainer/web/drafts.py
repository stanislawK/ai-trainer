"""The draft cards under a reply (PRD-0004 F2, ADR-0022, ticket #154): each pending draft sits
under the reply part that introduced it, the part after the athlete's log whose template is
that sport's extraction template. Its payload is read only through the plugin's model
(ADR-0022), and the sport-specific card body is the plugin's own partial."""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from pydantic import BaseModel, ValidationError

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.sports.base import SportPlugin
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.training import StoredDraft

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DraftCard:
    draft: StoredDraft
    sport: SportPlugin
    payload: BaseModel
    # Whether the draft's day is the athlete's today, so an assumed date can say "today".
    is_today: bool

    @property
    def body_template(self) -> str:
        return f"partials/chat/draft_{self.sport.id}.html"


def draft_cards(
    messages: Sequence[ChatMessage],
    drafts: Iterable[StoredDraft],
    *,
    sports: SportRegistry,
    today: date,
    log_id: UUID | None = None,
) -> Mapping[UUID, Sequence[DraftCard]]:
    """The cards to show under each assistant message, keyed by its ID. `log_id` is the
    athlete's message that `messages` answer, when the list starts with its reply."""
    pending: dict[tuple[UUID, str], list[StoredDraft]] = {}
    for draft in drafts:
        pending.setdefault((draft.message_id, draft.sport_id), []).append(draft)
    cards: dict[UUID, list[DraftCard]] = {}
    log = log_id
    for message in messages:
        if message.role is ChatRole.USER:
            log = message.id
            continue
        if log is None:
            continue
        for sport_id in message.sports:
            if not _is_extraction(message, sport_id, sports):
                continue
            for draft in pending.pop((log, sport_id), []):
                card = _card(draft, message, sports=sports, today=today)
                if card is not None:
                    cards.setdefault(message.id, []).append(card)
    return cards


def _is_extraction(part: ChatMessage, sport_id: str, sports: SportRegistry) -> bool:
    """Whether `part` is `sport_id`'s extraction reply, the only part a draft goes under."""
    try:
        spec = sports.get(sport_id).extraction
    except UnknownSportError:
        logger.warning("reply part %s has an unknown sport %r; no card", part.id, sport_id)
        return False
    return spec is not None and part.template_id == spec.template_id


def _card(
    draft: StoredDraft, part: ChatMessage, *, sports: SportRegistry, today: date
) -> DraftCard | None:
    """`None` when its stored payload no longer reads: the page still renders, without that
    card. `part` is already the sport's extraction reply (`_is_extraction`)."""
    sport = sports.get(draft.sport_id)
    spec = sport.extraction
    assert spec is not None
    assert part.template_id == spec.template_id
    try:
        payload = spec.payload_type.model_validate(draft.draft.payload)
    except ValidationError:
        logger.warning("draft %s has a %s payload that doesn't read; not shown", draft.id, sport.id)
        return None
    return DraftCard(
        draft=draft, sport=sport, payload=payload, is_today=draft.draft.occurred_on == today
    )
