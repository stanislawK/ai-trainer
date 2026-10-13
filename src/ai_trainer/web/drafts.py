"""The draft cards under a reply (PRD-0004 F2, ADR-0022, tickets #154 to #158): each
draft sits under the reply part that introduced it, the part after the athlete's log whose
template is that sport's extraction template. A pending one is the card, a confirmed one its
"Logged … to <date>" row, a discarded one its "Draft discarded" row and an undone one its
"Removed" row. A pending one opens as its edit form (#158). Its payload is read only through
the plugin's model (ADR-0022), and the sport-specific card body, edit fields and summary are
the plugin's own partials."""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from pydantic import BaseModel, JsonValue, ValidationError

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.sports.base import SportPlugin
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.training import StoredDraft

logger = logging.getLogger(__name__)

# An edit form posts at most this many rows of one list, e.g. a climbing draft's activities.
_MAX_FORM_ROWS = 50


@dataclass(frozen=True, slots=True)
class DraftCard:
    draft: StoredDraft
    sport: SportPlugin
    # `None` only for a resolved draft whose payload no longer reads: its row still shows, a
    # confirmed one saying "session" (ticket #155).
    payload: BaseModel | None
    # Whether the draft's day is the athlete's today, so an assumed date can say "today".
    is_today: bool

    @property
    def body_template(self) -> str:
        return f"partials/chat/draft_{self.sport.id}.html"

    @property
    def edit_template(self) -> str:
        """The sport's own fields in the edit form (ticket #158)."""
        return f"partials/chat/edit_{self.sport.id}.html"

    @property
    def summary_template(self) -> str:
        """The confirmed row's "what was logged", e.g. `Hades 8a` (the Chat mock's state 9)."""
        return f"partials/chat/logged_{self.sport.id}.html"

    @property
    def template(self) -> str:
        """The pending card, or the row a confirm, a discard or an undo collapses it to
        (tickets #155, #156 and #157). A confirmed draft without a session was undone."""
        if self.draft.status == "confirmed" and self.draft.session_id is None:
            return "partials/chat/draft_removed.html"
        if self.draft.status == "confirmed":
            return "partials/chat/draft_confirmed.html"
        if self.draft.status == "discarded":
            return "partials/chat/draft_discarded.html"
        return "partials/chat/draft_card.html"


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
                card = draft_card(draft, sports=sports, today=today)
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


def draft_card(draft: StoredDraft, *, sports: SportRegistry, today: date) -> DraftCard | None:
    """`None` when its sport is unknown, or when a pending draft's stored payload no longer
    reads: the page still renders, without that card."""
    try:
        sport = sports.get(draft.sport_id)
    except UnknownSportError:
        logger.warning("draft %s has an unknown sport %r; not shown", draft.id, draft.sport_id)
        return None
    spec = sport.extraction
    if spec is None:
        logger.warning("draft %s is of %s, which has no extraction; not shown", draft.id, sport.id)
        return None
    try:
        payload: BaseModel | None = spec.payload_type.model_validate(draft.draft.payload)
    except ValidationError:
        if draft.status == "pending":
            logger.warning(
                "draft %s has a %s payload that doesn't read; not shown", draft.id, sport.id
            )
            return None
        logger.warning(
            "%s draft %s has a %s payload that doesn't read", draft.status, draft.id, sport.id
        )
        payload = None
    return DraftCard(
        draft=draft, sport=sport, payload=payload, is_today=draft.draft.occurred_on == today
    )


def form_changes(pairs: Iterable[tuple[str, str]]) -> dict[str, JsonValue]:
    """The edit form's fields as the plugin's edit reads them (ticket #158): `rpe` stays a
    field, and `activities.0.count` becomes the `count` of row 0 of `activities`. A key that is
    neither, or a row past the form's limit, is dropped."""
    changes: dict[str, JsonValue] = {}
    rows: dict[str, dict[int, dict[str, JsonValue]]] = {}
    for key, value in pairs:
        parts = key.split(".")
        if len(parts) == 1:
            changes[key] = value
            continue
        if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) >= _MAX_FORM_ROWS:
            continue
        name, index, field = parts
        rows.setdefault(name, {}).setdefault(int(index), {})[field] = value
    for name, by_index in rows.items():
        changes[name] = [by_index.get(index, {}) for index in range(max(by_index) + 1)]
    return changes
