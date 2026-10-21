"""Which line of a reply a choice card replaces (PRD-0004 F9, ADR-0015, ticket #162): the
question's own reply line after the athlete's log, once per question, and the option an
answered card shows."""

from datetime import UTC, datetime
from uuid import uuid4

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    SPORT_QUESTION_TEMPLATE_ID,
    ChoiceOption,
    Clarification,
    StoredClarification,
)
from ai_trainer.web.clarifications import ChoiceCard, choice_cards

_NOW = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
_CONVERSATION = uuid4()
_QUESTION = Clarification(
    question="Which sport was that?",
    options=(
        ChoiceOption(id="gym", copy_key="sport.gym.label"),
        ChoiceOption(id="cycling", copy_key="sport.cycling.label"),
    ),
    pending="log_session",
)


def _message(role: ChatRole, template_id: str | None = None) -> ChatMessage:
    return ChatMessage(
        id=uuid4(),
        conversation_id=_CONVERSATION,
        role=role,
        text="text",
        created_at=_NOW,
        template_id=template_id,
    )


def _stored(log: ChatMessage, **changes: object) -> StoredClarification:
    return StoredClarification(
        id=uuid4(),
        conversation_id=_CONVERSATION,
        message_id=log.id,
        purpose="sport",
        clarification=_QUESTION,
        status="open",
        created_at=_NOW,
    ).model_copy(update=changes)


def test_the_card_replaces_the_question_line_after_its_log() -> None:
    log = _message(ChatRole.USER)
    line = _message(ChatRole.ASSISTANT, SPORT_QUESTION_TEMPLATE_ID)
    question = _stored(log)

    cards = choice_cards([log, line], [question])

    assert cards == {line.id: ChoiceCard(question)}


def test_a_question_goes_under_one_line_only() -> None:
    log = _message(ChatRole.USER)
    first = _message(ChatRole.ASSISTANT, SPORT_QUESTION_TEMPLATE_ID)
    second = _message(ChatRole.ASSISTANT, SPORT_QUESTION_TEMPLATE_ID)

    assert list(choice_cards([log, first, second], [_stored(log)])) == [first.id]


def test_another_replys_lines_and_other_questions_get_no_card() -> None:
    log, other_log = _message(ChatRole.USER), _message(ChatRole.USER)
    line = _message(ChatRole.ASSISTANT, SPORT_QUESTION_TEMPLATE_ID)
    chat = _message(ChatRole.ASSISTANT, "chitchat")

    assert choice_cards([log, chat], [_stored(log)]) == {}
    assert choice_cards([other_log, line], [_stored(log)]) == {}


def test_a_window_that_opens_on_the_reply_names_its_log() -> None:
    log = _message(ChatRole.USER)
    line = _message(ChatRole.ASSISTANT, SPORT_QUESTION_TEMPLATE_ID)

    assert list(choice_cards([line], [_stored(log)], log_id=log.id)) == [line.id]
    assert choice_cards([line], [_stored(log)]) == {}


def test_an_answered_card_names_the_option_that_was_chosen() -> None:
    log = _message(ChatRole.USER)

    chosen = ChoiceCard(_stored(log, status="answered", answer_option_ids=("cycling",))).chosen

    assert chosen == ChoiceOption(id="cycling", copy_key="sport.cycling.label")


def test_a_card_with_no_known_answer_names_no_option() -> None:
    log = _message(ChatRole.USER)

    assert ChoiceCard(_stored(log)).chosen is None
    assert ChoiceCard(_stored(log, answer_option_ids=("rowing",))).chosen is None
