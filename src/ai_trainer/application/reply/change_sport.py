"""A pending draft's sport changed on its card (B23, ticket #163): the same log extracted again
in the new sport, which replaces the draft in place."""

import asyncio
from collections.abc import Collection, Iterable, Sequence
from dataclasses import replace
from uuid import UUID

from ai_trainer.application.drafts import draft_to_edit
from ai_trainer.application.reply.extract import (
    extract,
    same_session_question,
    session_continuity,
)
from ai_trainer.application.reply.shared import (
    ReplyFailed,
    ReplyServices,
    extractable,
    find_message,
    reply_context,
)
from ai_trainer.domain.clarification import READING_PURPOSE, StoredClarification
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import DraftSportNotOfferedError, StoredDraft


def sports_to_switch_to(
    athlete_sports: Sequence[str],
    current: str,
    sports: SportRegistry,
    *,
    asking: Collection[str] = (),
) -> tuple[str, ...]:
    """The sports a draft's card offers to move it to (B23, ticket #163): the athlete's own, in
    their order, that name an extraction template, without the draft's own sport, nor one whose
    reading question is still open on the same log (`asking`), which a switch would answer
    with a guess (ticket #164)."""
    loggable = extractable(sports)
    return tuple(
        sport
        for sport in athlete_sports
        if sport in loggable and sport != current and sport not in asking
    )


def asking_sports(questions: Iterable[StoredClarification], message_id: UUID) -> frozenset[str]:
    """The sports whose reading question is still open on the athlete's log (ticket #164)."""
    return frozenset(
        question.clarification.sport
        for question in questions
        if question.message_id == message_id
        and question.purpose == READING_PURPOSE
        and question.status == "open"
        and question.clarification.sport is not None
    )


async def change_draft_sport(
    user_id: UUID,
    draft_id: UUID,
    sport_id: str,
    *,
    timezone: str,
    services: ReplyServices,
) -> StoredDraft | ReplyFailed:
    """The card's sport chip (B23, F2, ticket #163): re-runs `sport_id`'s extraction on the
    draft's own log and replaces the pending draft in place, offered again as an addition to
    the new sport's latest session when it came close after it (B16). No session is written
    (ADR-0022 invariant 3), and a failed extraction changes nothing. A same-session question
    on the card is dropped, and the new sport's own is asked when it came later that day
    (ticket #166). Raises `DraftNotFoundError`, `DraftConfirmedError` or `DraftDiscardedError`,
    and `DraftSportNotOfferedError` for a sport the card never offered (nothing is called then),
    and `MessageNotFoundError` for a log past its conversation's retention."""
    stored = await draft_to_edit(user_id, draft_id, drafts=services.drafts)
    asked = await services.clarifications.list_shown(user_id, stored.conversation_id)
    offered = sports_to_switch_to(
        await services.profile.list_sports(user_id),
        stored.sport_id,
        services.sports,
        asking=asking_sports(asked, stored.message_id),
    )
    if sport_id not in offered:
        raise DraftSportNotOfferedError(sport_id)
    message = await find_message(
        user_id, stored.message_id, chat=services.chat, conversations=services.conversations
    )
    # Read as of when the log was sent, as its first extraction was, so a switch after
    # midnight still dates "yesterday" or an unstated day from the log (ADR-0014).
    context = replace(
        await reply_context(user_id, message, timezone, services), now=message.created_at
    )
    # The card's own fields fix how the log reads, so the switch never asks (ticket #164).
    answer = await extract(context, sport_id, services, may_ask=False)
    if isinstance(answer, ReplyFailed):
        return answer
    assert answer.draft is not None  # an extraction that may not ask always drafts
    _, draft, payload_version = answer.draft

    async def replace_draft() -> StoredDraft:
        kind, latest = await session_continuity(user_id, message, sport_id, draft, services)
        return await services.drafts.change_sport(
            user_id,
            draft_id,
            sport_id,
            draft,
            payload_version=payload_version,
            extends_session_id=latest.id if kind == "join" and latest is not None else None,
            asks=same_session_question(draft, latest, services)
            if kind == "ask" and latest is not None
            else None,
            asked_at=services.clock.now(),
            template_id=answer.template.id,
            template_version=answer.template.version,
        )

    # Saved in its own task: a reader who leaves now can't cut the replacement off.
    return await asyncio.shield(asyncio.ensure_future(replace_draft()))
