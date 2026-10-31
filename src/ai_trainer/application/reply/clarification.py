"""The questions a reply asks (ADR-0015): the sport question on a log whose sport stays split
(B17, B23), resumed by the tap; a reading question, which resumes its sport's extraction with
the pick (ticket #164); and a draft's same-session question, which the tap settles (B16,
ticket #166)."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from ai_trainer.application.reply.extract import extract, save
from ai_trainer.application.reply.shared import (
    LOG_KIND,
    TICK,
    ReplyDone,
    ReplyFailed,
    ReplyServices,
    find_message,
    reply_context,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    NEW_SESSION_OPTION,
    READING_PURPOSE,
    SAME_SESSION_PURPOSE,
    SPORT_PURPOSE,
    ChoiceOption,
    Clarification,
    ClarificationNotFoundError,
    ClarificationResolvedError,
    StoredClarification,
    UnknownOptionError,
)
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.training import StoredDraft


@dataclass(frozen=True, slots=True)
class SessionSettled:
    """A same-session question as answered: the pending draft, now extending the session the
    athlete picked, or none (ticket #166)."""

    draft: StoredDraft


@dataclass(frozen=True, slots=True)
class ClarificationAnswered:
    """The question as answered, and the reply its answer resumed: the draft's intro line and
    the pending draft in the chosen sport."""

    clarification: StoredClarification
    reply: ReplyDone


async def ask_sport(
    user_id: UUID, message: ChatMessage, sports: Sequence[str], services: ReplyServices
) -> ReplyDone:
    """The question as the reply to the athlete's log, and its open `clarifications` row. No
    draft and no session is written before the athlete picks (ADR-0015 invariant 3)."""
    question = services.conversation.sport_question()
    saved = await services.chat.add(
        user_id,
        message.conversation_id,
        ChatRole.ASSISTANT,
        question.text,
        message.created_at + TICK,
        template_id=question.template.id,
        template_version=question.template.version,
    )
    asked = await services.clarifications.add(
        user_id,
        message.conversation_id,
        message.id,
        SPORT_PURPOSE,
        Clarification(
            question=question.text,
            options=tuple(
                ChoiceOption(id=sport, copy_key=services.sports.get(sport).label_key)
                for sport in sports
            ),
            pending=LOG_KIND,
        ),
        created_at=services.clock.now(),
    )
    await services.conversations.touch(user_id, message.conversation_id, services.clock.now())
    return ReplyDone(messages=[saved], clarifications=(asked,))


def _chosen(question: StoredClarification, option_ids: Sequence[str]) -> str:
    """The one option the athlete tapped. The option ID is trusted, label text never is: an ID
    the question did not offer is an error, not a guess (ADR-0015 invariant 4)."""
    offered = {option.id for option in question.clarification.options}
    if len(option_ids) != 1 or option_ids[0] not in offered:
        raise UnknownOptionError(option_ids)
    return option_ids[0]


async def answer_clarification(
    user_id: UUID,
    clarification_id: UUID,
    option_ids: Sequence[str],
    *,
    timezone: str,
    services: ReplyServices,
) -> ClarificationAnswered | SessionSettled | ReplyFailed:
    """Resumes the extraction the question blocked with the tapped sport (B23, ADR-0015), or,
    for a reading question, in the sport that asked with the tapped reading, which can then only
    draft (ticket #164). The question is answered, and its reply stored, only once the
    extraction came through, so a failed one leaves the card to tap again; two taps at once
    leave one draft, because the second finds the question already answered before anything is
    written. A sport answered may ask how to read the log next. A same-session question settles
    its draft instead (ticket #166). Raises `ClarificationNotFoundError`,
    `ClarificationResolvedError` or `UnknownOptionError` (nothing is called or written then),
    and `MessageNotFoundError` for a log past its conversation's retention."""
    question = await services.clarifications.get(user_id, clarification_id)
    if question.purpose not in (SPORT_PURPOSE, READING_PURPOSE, SAME_SESSION_PURPOSE):
        raise ClarificationNotFoundError(clarification_id)
    if question.purpose == READING_PURPOSE and question.clarification.sport is None:
        raise ClarificationNotFoundError(clarification_id)
    if question.status != "open":
        raise ClarificationResolvedError(clarification_id)
    if question.purpose == SAME_SESSION_PURPOSE:
        settled = await _settle_session(user_id, question, option_ids, services)
        await _record_choice(
            user_id,
            question,
            template_id=settled.draft.template_id,
            template_version=settled.draft.template_version,
            services=services,
        )
        return settled
    picked = _chosen(question, option_ids)
    reading = question.purpose == READING_PURPOSE
    sport = question.clarification.sport if reading else picked
    assert sport is not None  # a reading question names its sport, checked above
    message = await find_message(
        user_id, question.message_id, chat=services.chat, conversations=services.conversations
    )
    # Read as of when the log was sent, as a sport change and a retry are, so an answer the
    # next morning still dates "last night" from the log (ADR-0014, ticket #166).
    context = replace(
        await reply_context(user_id, message, timezone, services), now=message.created_at
    )
    option = next(option for option in question.clarification.options if option.id == picked)
    answer = await extract(context, sport, services, answer=option if reading else None)
    if isinstance(answer, ReplyFailed):
        return answer

    async def resume() -> ClarificationAnswered:
        resolved = await services.clarifications.resolve(
            user_id,
            clarification_id,
            "answered",
            option_ids=[picked],
            resolved_at=services.clock.now(),
        )
        asked = await services.chat.list_replies(user_id, message)
        try:
            reply = await save(
                user_id,
                message,
                [answer],
                services,
                after=max((part.created_at for part in asked), default=None),
            )
        except BaseException:
            # The draft did not come through, so the card goes back to be tapped again.
            await asyncio.shield(services.clarifications.reopen(user_id, clarification_id))
            raise
        return ClarificationAnswered(clarification=resolved, reply=reply)

    # Saved in its own task: a reader who leaves now can't cut the draft off after the answer.
    resumed = await asyncio.shield(asyncio.ensure_future(resume()))
    await _record_choice(
        user_id,
        question,
        template_id=answer.template.id,
        template_version=answer.template.version,
        services=services,
    )
    return resumed


async def _record_choice(
    user_id: UUID,
    question: StoredClarification,
    *,
    template_id: str | None,
    template_version: int | None,
    services: ReplyServices,
) -> None:
    """One `choice_answered` feedback event (ADR-0034): the template of what the answer made,
    and the question's purpose as its only field; never the option picked or any text."""
    if services.feedback is not None:
        await services.feedback.record(
            user_id,
            FeedbackKind.CHOICE_ANSWERED,
            template_id=template_id,
            template_version=template_version,
            fields=(question.purpose,),
        )


async def _settle_session(
    user_id: UUID,
    question: StoredClarification,
    option_ids: Sequence[str],
    services: ReplyServices,
) -> SessionSettled:
    """The tap on a draft's same-session question (B16, ticket #166): "Add" makes the draft
    extend the session the option names, "New session" makes it extend none. Nothing else is
    written; the athlete still confirms (ADR-0014 invariant 5)."""
    picked = _chosen(question, option_ids)
    if question.draft_id is None:
        raise ClarificationNotFoundError(question.id)
    draft = await services.drafts.settle_session(
        user_id,
        question.draft_id,
        question.id,
        picked,
        extends_session_id=None if picked == NEW_SESSION_OPTION else UUID(picked),
        answered_at=services.clock.now(),
    )
    return SessionSettled(draft=draft)


async def abandon_clarification(
    user_id: UUID, clarification_id: UUID, *, services: ReplyServices
) -> StoredClarification:
    """ "Let me type instead" (F9): closes the question with no answer, and nothing else is
    written. A question that offers no such escape, as a same-session one (ticket #166), can't
    be skipped this way, so it is not found. Raises `ClarificationNotFoundError` or
    `ClarificationResolvedError`."""
    question = await services.clarifications.get(user_id, clarification_id)
    if not question.clarification.allow_free_text:
        raise ClarificationNotFoundError(clarification_id)
    return await services.clarifications.resolve(
        user_id,
        clarification_id,
        "abandoned",
        option_ids=None,
        resolved_at=services.clock.now(),
    )
