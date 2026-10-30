"""A failed draft's Retry (B1, B14, ticket #161): a log of several sports keeps a sport whose
extraction failed beside a part that came through as a line, and its Retry runs that sport's
extraction again and turns the line into the draft."""

import asyncio
from dataclasses import dataclass, replace
from uuid import UUID

from ai_trainer.application.reply.extract import extract, store_draft
from ai_trainer.application.reply.shared import (
    FailedPartNotFoundError,
    ReplyFailed,
    ReplyServices,
    find_message,
    reply_context,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.training import DRAFT_FAILED_TEMPLATE_ID, StoredDraft


@dataclass(frozen=True, slots=True)
class RetriedDraft:
    """A failed draft's part as the retry rewrote it, the draft's intro line, and the pending
    draft it now introduces."""

    part: ChatMessage
    draft: StoredDraft


async def _failed_part(
    user_id: UUID, part_id: UUID, services: ReplyServices
) -> tuple[ChatMessage, ChatMessage]:
    """The user's failed draft's part and the log it replies to: the user message the reply's
    parts follow. Raises `FailedPartNotFoundError` for any other part, and
    `MessageNotFoundError` for a log past its conversation's retention."""
    part = await services.chat.get(user_id, part_id)
    if (
        part is None
        or part.role is not ChatRole.ASSISTANT
        or part.template_id != DRAFT_FAILED_TEMPLATE_ID
        or len(part.sports) != 1
    ):
        raise FailedPartNotFoundError(part_id)
    before = part
    while earlier := await services.chat.list_before(user_id, before, 1):
        before = earlier[0]
        if before.role is ChatRole.USER:
            log = await find_message(
                user_id, before.id, chat=services.chat, conversations=services.conversations
            )
            return part, log
    raise FailedPartNotFoundError(part_id)


@dataclass(frozen=True, slots=True)
class RetryFailed:
    """The retried extraction failed again: the part as it stays, to show its row again."""

    part: ChatMessage
    outcome: LlmCallOutcome


async def retry_draft(
    user_id: UUID, part_id: UUID, *, timezone: str, services: ReplyServices
) -> RetriedDraft | RetryFailed:
    """A failed draft's Retry (ticket #161): runs its sport's extraction again on its log, as of
    when the log was sent, and turns the part into the draft's intro line in place, with the
    pending draft under it, offered as an addition to a session close before it (B16), or
    asking whether it is one from earlier that day (ticket #166). No
    session is written (ADR-0022 invariant 3), and a failed extraction changes nothing. Two
    retries at once leave one draft: the second finds the part already rewritten. Raises
    `FailedPartNotFoundError` for a part that isn't the user's failed draft (nothing is called
    then), and `MessageNotFoundError` for a log past its conversation's retention."""
    failed, message = await _failed_part(user_id, part_id, services)
    sport_id = failed.sports[0]
    # Read as of when the log was sent, as its first extraction was (ADR-0014).
    context = replace(
        await reply_context(user_id, message, timezone, services), now=message.created_at
    )
    # A retry turns its line into a draft, never a question (ticket #164).
    answer = await extract(context, sport_id, services, may_ask=False)
    if isinstance(answer, ReplyFailed):
        return RetryFailed(part=failed, outcome=answer.outcome)
    assert answer.draft is not None  # an extraction that may not ask always drafts
    _, draft, payload_version = answer.draft

    async def save() -> RetriedDraft:
        part = await services.chat.replace_part(
            user_id,
            part_id,
            expected_template_id=DRAFT_FAILED_TEMPLATE_ID,
            text=answer.text,
            template_id=answer.template.id,
            template_version=answer.template.version,
        )
        if part is None:
            raise FailedPartNotFoundError(part_id)
        try:
            stored, _ = await store_draft(
                user_id,
                message,
                sport_id=sport_id,
                draft=draft,
                payload_version=payload_version,
                services=services,
            )
        except BaseException:
            # The draft did not come through, so the part goes back to offer Retry again.
            fixed = services.conversation.draft_failed()
            await asyncio.shield(
                services.chat.replace_part(
                    user_id,
                    part_id,
                    expected_template_id=answer.template.id,
                    text=fixed.text,
                    template_id=fixed.template.id,
                    template_version=fixed.template.version,
                )
            )
            raise
        await services.conversations.touch(user_id, message.conversation_id, services.clock.now())
        return RetriedDraft(part=part, draft=stored)

    # Saved in its own task: a reader who leaves now can't cut the draft off after its line.
    return await asyncio.shield(asyncio.ensure_future(save()))
