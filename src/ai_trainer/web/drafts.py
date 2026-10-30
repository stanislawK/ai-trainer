"""The draft cards under a reply (PRD-0004 F2, ADR-0022, tickets #154 to #158): each
draft sits under the reply part that introduced it, the part after the athlete's log whose
template is that sport's extraction template. A pending one is the card, a confirmed one its
"Logged … to <date>" row, a discarded one its "Draft discarded" row and an undone one its
"Removed" row. A pending one opens as its edit form (#158). One that came close after a
session offers to add to it, and once confirmed says it was added (#165). A pending one
offers the athlete's other sports on its sport chip, and one moved to another sport stays
under the reply it came with (#163). One that came later on the day of a same-sport session
asks on its card whether it is that session, and waits for the pick to be confirmed (#166). Its
payload is read only through the plugin's model
(ADR-0022), and the sport-specific card body, edit fields and summary are the plugin's own
partials."""

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, JsonValue, ValidationError

from ai_trainer.application.reply import asking_sports, sports_to_switch_to
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import SAME_SESSION_PURPOSE, StoredClarification
from ai_trainer.domain.sports.base import SportPlugin
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.training import DRAFT_FAILED_TEMPLATE_ID, StoredDraft, joins_day

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
    # The sports a pending draft's chip offers to move it to (B23, ticket #163); none for a
    # one-sport athlete or a resolved draft.
    switch_to: tuple[SportPlugin, ...] = ()
    # The same-session question still open on a pending draft's card (B16, ticket #166).
    question: StoredClarification | None = None

    @property
    def adds_at(self) -> str | None:
        """When the session this draft adds to began, as `HH:MM` in the draft's own timezone
        (ADR-0014): "Add to the session at 11:10" (B16). `None` when it extends no session, or
        no longer can, since the athlete edited it to another day."""
        extends = self.draft.extends
        if extends is None or not joins_day(self.draft.draft, extends.occurred_on):
            return None
        local = extends.first_logged_at.astimezone(ZoneInfo(self.draft.draft.timezone))
        return f"{local:%H:%M}"

    @property
    def added(self) -> bool:
        """Whether the confirm added this draft to the session it extends (B16)."""
        extends = self.draft.extends
        return extends is not None and self.draft.session_id == extends.id

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


def open_questions(
    questions: Iterable[StoredClarification],
) -> Mapping[UUID, StoredClarification]:
    """The same-session questions still open, by the draft whose card asks them (#166)."""
    return {
        question.draft_id: question
        for question in questions
        if question.draft_id is not None
        and question.purpose == SAME_SESSION_PURPOSE
        and question.status == "open"
    }


def draft_cards(  # noqa: C901 -- split in #333
    messages: Sequence[ChatMessage],
    drafts: Iterable[StoredDraft],
    *,
    sports: SportRegistry,
    today: date,
    athlete_sports: Sequence[str] = (),
    log_id: UUID | None = None,
    questions: Iterable[StoredClarification] = (),
) -> Mapping[UUID, Sequence[DraftCard]]:
    """The cards to show under each assistant message, keyed by its ID. `log_id` is the
    athlete's message that `messages` answer, when the list starts with its reply. A draft
    moved to a sport its log's reply never extracted goes under that reply's last extraction
    part (#163). `questions` are the conversation's, and a pending card shows its own open one
    (#166)."""
    pending: dict[tuple[UUID, str], list[StoredDraft]] = {}
    for draft in drafts:
        pending.setdefault((draft.message_id, draft.sport_id), []).append(draft)
    cards: dict[UUID, list[DraftCard]] = {}
    last_extraction: dict[UUID, UUID] = {}
    shown = list(questions)
    asking = open_questions(shown)

    def place(message_id: UUID, found: Iterable[StoredDraft]) -> None:
        for draft in found:
            card = draft_card(
                draft,
                sports=sports,
                today=today,
                athlete_sports=athlete_sports,
                question=asking.get(draft.id),
                asking=asking_sports(shown, draft.message_id),
            )
            if card is not None:
                cards.setdefault(message_id, []).append(card)

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
            last_extraction[log] = message.id
            place(message.id, pending.pop((log, sport_id), []))
    for (log_of, _), moved in pending.items():
        if log_of in last_extraction:
            place(last_extraction[log_of], moved)
    return cards


def _is_extraction(part: ChatMessage, sport_id: str, sports: SportRegistry) -> bool:
    """Whether `part` is `sport_id`'s extraction reply, the only part a draft goes under."""
    try:
        spec = sports.get(sport_id).extraction
    except UnknownSportError:
        logger.warning("reply part %s has an unknown sport %r; no card", part.id, sport_id)
        return False
    return spec is not None and part.template_id == spec.template_id


def build_failed_draft_sport(
    sports: SportRegistry,
) -> Callable[[ChatMessage], SportPlugin | None]:
    """The sport of a reply part saved in place of a draft whose extraction failed, which the
    chat shows as a row with Retry (ticket #161); `None` for any other part, or one whose sport
    the registry no longer knows."""

    def failed_draft_sport(message: ChatMessage) -> SportPlugin | None:
        if message.template_id != DRAFT_FAILED_TEMPLATE_ID or len(message.sports) != 1:
            return None
        try:
            return sports.get(message.sports[0])
        except UnknownSportError:
            logger.warning("failed draft %s has an unknown sport %r", message.id, message.sports[0])
            return None

    return failed_draft_sport


def draft_card(
    draft: StoredDraft,
    *,
    sports: SportRegistry,
    today: date,
    athlete_sports: Sequence[str] = (),
    question: StoredClarification | None = None,
    asking: Collection[str] = (),
) -> DraftCard | None:
    """`None` when its sport is unknown, or when a pending draft's stored payload no longer
    reads: the page still renders, without that card. A pending card's chip offers
    `athlete_sports`, as `sports_to_switch_to` narrows them, without the sports still `asking`
    on its log (#164), and asks `question` (#166)."""
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
    switch_to = (
        tuple(
            sports.get(sport_id)
            for sport_id in sports_to_switch_to(athlete_sports, sport.id, sports, asking=asking)
        )
        if draft.status == "pending"
        else ()
    )
    return DraftCard(
        draft=draft,
        sport=sport,
        payload=payload,
        is_today=draft.draft.occurred_on == today,
        switch_to=switch_to,
        question=question if draft.status == "pending" else None,
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
