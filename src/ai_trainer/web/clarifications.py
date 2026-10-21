"""The choice cards under a reply (PRD-0004 B17, F9, ADR-0015, ticket #162): a question sits
where its reply line is, the part after the athlete's log whose template is the question's
fixed reply. Open it is the card, answered it is the row that shows the chosen option, and
abandoned ("let me type instead") it is a quiet row. A sport question and a reading question
(ticket #164) are both asked this way."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    READING_PURPOSE,
    READING_QUESTION_TEMPLATE_ID,
    SPORT_PURPOSE,
    SPORT_QUESTION_TEMPLATE_ID,
    ChoiceOption,
    StoredClarification,
)

# The fixed reply a purpose's question is sent as.
_QUESTION_TEMPLATES = {
    SPORT_PURPOSE: SPORT_QUESTION_TEMPLATE_ID,
    READING_PURPOSE: READING_QUESTION_TEMPLATE_ID,
}


@dataclass(frozen=True, slots=True)
class ChoiceCard:
    clarification: StoredClarification

    @property
    def chosen(self) -> ChoiceOption | None:
        """The option an answered question was answered with."""
        answered = self.clarification.answer_option_ids
        if not answered:
            return None
        return next(
            (
                option
                for option in self.clarification.clarification.options
                if option.id == answered[0]
            ),
            None,
        )


def choice_cards(
    messages: Sequence[ChatMessage],
    clarifications: Iterable[StoredClarification],
    *,
    log_id: UUID | None = None,
) -> Mapping[UUID, ChoiceCard]:
    """The card for each question line in `messages`, keyed by the line's ID. `log_id` is the
    athlete's message that `messages` answer, when the list starts with its reply."""
    open_for_log: dict[tuple[UUID, str], StoredClarification] = {}
    for question in clarifications:
        open_for_log.setdefault((question.message_id, question.purpose), question)
    templates = {template: purpose for purpose, template in _QUESTION_TEMPLATES.items()}
    cards: dict[UUID, ChoiceCard] = {}
    log = log_id
    for message in messages:
        if message.role is ChatRole.USER:
            log = message.id
            continue
        purpose = templates.get(message.template_id or "")
        if log is None or purpose is None:
            continue
        if (log, purpose) in open_for_log:
            cards[message.id] = ChoiceCard(open_for_log.pop((log, purpose)))
    return cards
