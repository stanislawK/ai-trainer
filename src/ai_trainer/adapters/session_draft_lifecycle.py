"""The lifecycle of a stored draft (ADR-0022): confirm, settle the same-session question, discard
and undo. Each function runs one transaction on the `AsyncSession` it is given and commits it;
`SqlAlchemySessionDraftRepository` opens the session and delegates here (ticket #346). Every
function locks the draft row first, then the training session, so two of them on one draft or
one session wait for each other."""

from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.activity_stores import ActivityStore
from ai_trainer.adapters.orm import (
    ChatMessageOrm,
    ClarificationOrm,
    SessionDraftOrm,
    TrainingSessionOrm,
    TrainingSessionSourceOrm,
)
from ai_trainer.adapters.training_session_repository import spans_of
from ai_trainer.application.ports.activities import LoggedActivities
from ai_trainer.domain.clarification import ClarificationResolvedError
from ai_trainer.domain.sports.base import ExtractionSpec
from ai_trainer.domain.training import (
    DraftAwaitingAnswerError,
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
    DraftStatus,
    SessionDraft,
    SessionNotFoundError,
    SessionSpan,
    StoredDraft,
    joins_day,
)

PENDING: DraftStatus = "pending"
CONFIRMED: DraftStatus = "confirmed"
DISCARDED: DraftStatus = "discarded"
# A question on a draft's card is open until the athlete picks (ticket #166).
OPEN = "open"
_CHAT_SOURCE = "chat"
# The envelope fields an addition to a session fills when the session has none (ticket #165).
_FILLED_FIELDS = ("started_at", "duration_min", "rpe", "feeling", "notes")


def to_domain(row: SessionDraftOrm, spans: Mapping[UUID, SessionSpan]) -> StoredDraft:
    extends = None if row.extends_session_id is None else spans.get(row.extends_session_id)
    return StoredDraft.model_validate(
        {
            "id": row.id,
            "conversation_id": row.conversation_id,
            "message_id": row.message_id,
            "sport_id": row.sport_id,
            "draft": SessionDraft.model_validate(row.draft),
            "payload_version": row.payload_version,
            "status": row.status,
            "session_id": row.session_id,
            "extends": extends,
            "template_id": row.template_id,
            "template_version": row.template_version,
            "created_at": row.created_at,
        }
    )


async def with_span(session: AsyncSession, user_id: UUID, row: SessionDraftOrm) -> StoredDraft:
    ids = [] if row.extends_session_id is None else [row.extends_session_id]
    return to_domain(row, await spans_of(session, user_id, ids))


async def _locked_draft(session: AsyncSession, user_id: UUID, draft_id: UUID) -> SessionDraftOrm:
    row = await session.scalar(
        select(SessionDraftOrm)
        .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
        .with_for_update()
    )
    if row is None:
        raise DraftNotFoundError(draft_id)
    return row


async def _awaits_answer(session: AsyncSession, user_id: UUID, draft_id: UUID) -> bool:
    """Whether a question on the user's draft's card is still open (ticket #166)."""
    open_id = await session.scalar(
        select(ClarificationOrm.id)
        .where(
            ClarificationOrm.user_id == user_id,
            ClarificationOrm.draft_id == draft_id,
            ClarificationOrm.status == OPEN,
        )
        .limit(1)
    )
    return open_id is not None


async def _extended(
    session: AsyncSession, user_id: UUID, row: SessionDraftOrm, draft: SessionDraft
) -> TrainingSessionOrm | None:
    """The session the draft extends, locked, while it exists, is in the draft's sport and
    the draft can still belong to its day (ADR-0014, ticket #165)."""
    if row.extends_session_id is None:
        return None
    training = await session.scalar(
        select(TrainingSessionOrm)
        .where(
            TrainingSessionOrm.id == row.extends_session_id,
            TrainingSessionOrm.user_id == user_id,
        )
        .with_for_update()
    )
    if (
        training is None
        or training.sport_id != row.sport_id
        or not joins_day(draft, training.occurred_on)
    ):
        return None
    return training


async def confirm(
    session: AsyncSession,
    user_id: UUID,
    draft_id: UUID,
    *,
    confirmed_at: datetime,
    spec_of: Callable[[str], ExtractionSpec],
    store_of: Callable[[str], ActivityStore],
) -> StoredDraft:
    """One transaction (ADR-0022): the session, or the addition to the one the draft
    extends (B16), its source row with the log's original text (B2), the activities in the
    sport's `ActivityStore` (ADR-0032), and the draft's move to `confirmed`. The draft row is
    locked first, so a second confirm waits for the first and then finds it done: one session
    either way. The extended session is locked next, as in
    an undo, so two additions to it land one after the other. A draft whose card still asks
    whether it is the same session is refused (ticket #166); the answer locks the draft row
    too, so a confirm racing it sees the session it settled."""
    row = await _locked_draft(session, user_id, draft_id)
    if row.status == DISCARDED:
        raise DraftDiscardedError(draft_id)
    if row.status == CONFIRMED:
        return await with_span(session, user_id, row)
    if await _awaits_answer(session, user_id, draft_id):
        raise DraftAwaitingAnswerError(draft_id)
    log = (
        await session.execute(
            select(ChatMessageOrm.text, ChatMessageOrm.created_at).where(
                ChatMessageOrm.id == row.message_id, ChatMessageOrm.user_id == user_id
            )
        )
    ).one()
    draft = SessionDraft.model_validate(row.draft)
    training = await _extended(session, user_id, row, draft)
    session_payload: dict[str, JsonValue] | None = None
    if training is None:
        # The session is gone, or the draft now names another day: a session of its own. Its
        # payload is the sport store's to set, below.
        row.extends_session_id = None
        training = TrainingSessionOrm(
            user_id=user_id,
            sport_id=row.sport_id,
            occurred_on=draft.occurred_on,
            timezone=draft.timezone,
            started_at=draft.started_at,
            duration_min=draft.duration_min,
            rpe=draft.rpe,
            feeling=draft.feeling,
            notes=draft.notes,
            payload={},
            payload_version=row.payload_version,
            created_at=confirmed_at,
            updated_at=confirmed_at,
        )
        session.add(training)
    else:
        session_payload = training.payload
        training.payload_version = spec_of(row.sport_id).payload_version
        for name in _FILLED_FIELDS:
            if getattr(training, name) is None:
                setattr(training, name, getattr(draft, name))
        training.updated_at = confirmed_at
    await session.flush()
    source = TrainingSessionSourceOrm(
        user_id=user_id,
        session_id=training.id,
        raw_text=log.text,
        source=_CHAT_SOURCE,
        logged_at=log.created_at,
    )
    session.add(source)
    await session.flush()
    # The sport's activities, in its own tables or the payload (ADR-0032), with the source row
    # they came from.
    training.payload = await store_of(row.sport_id).add(
        session,
        user_id,
        LoggedActivities(
            session_id=training.id,
            source_id=source.id,
            session_payload=session_payload,
            addition=draft.payload,
        ),
    )
    row.status = CONFIRMED
    row.session_id = training.id
    row.resolved_at = confirmed_at
    await session.flush()
    confirmed = await with_span(session, user_id, row)
    await session.commit()
    return confirmed


async def settle_session(
    session: AsyncSession,
    user_id: UUID,
    draft_id: UUID,
    clarification_id: UUID,
    option_id: str,
    *,
    extends_session_id: UUID | None,
    answered_at: datetime,
) -> StoredDraft:
    """One transaction (ticket #166): the draft row is locked first, as in a confirm, then
    the question moves from `open` to `answered` with one conditional UPDATE, so two taps
    at once settle it once, and the draft extends the picked session while it is the
    user's, in the draft's sport."""
    row = await _locked_draft(session, user_id, draft_id)
    if row.status == CONFIRMED:
        raise DraftConfirmedError(draft_id)
    if row.status == DISCARDED:
        raise DraftDiscardedError(draft_id)
    answered = await session.scalar(
        update(ClarificationOrm)
        .where(
            ClarificationOrm.user_id == user_id,
            ClarificationOrm.id == clarification_id,
            ClarificationOrm.draft_id == draft_id,
            ClarificationOrm.status == OPEN,
        )
        .values(status="answered", answer_option_ids=[option_id], answered_at=answered_at)
        .returning(ClarificationOrm.id)
    )
    if answered is None:
        raise ClarificationResolvedError(clarification_id)
    extended = None
    if extends_session_id is not None:
        extended = await session.scalar(
            select(TrainingSessionOrm.id).where(
                TrainingSessionOrm.id == extends_session_id,
                TrainingSessionOrm.user_id == user_id,
                TrainingSessionOrm.sport_id == row.sport_id,
            )
        )
    row.extends_session_id = extended
    await session.flush()
    settled = await with_span(session, user_id, row)
    await session.commit()
    return settled


async def discard(
    session: AsyncSession, user_id: UUID, draft_id: UUID, *, discarded_at: datetime
) -> StoredDraft:
    """Marks the draft `discarded`; it writes no session (ADR-0022 invariant 3). The row is
    locked first, so a discard racing a confirm either finds it confirmed and is refused,
    or goes first and makes the confirm refuse."""
    row = await _locked_draft(session, user_id, draft_id)
    if row.status == CONFIRMED:
        raise DraftConfirmedError(draft_id)
    if row.status == PENDING:
        row.status = DISCARDED
        row.resolved_at = discarded_at
    await session.flush()
    discarded = await with_span(session, user_id, row)
    await session.commit()
    return discarded


async def undo(
    session: AsyncSession,
    user_id: UUID,
    draft_id: UUID,
    *,
    undone_at: datetime,
    spec_of: Callable[[str], ExtractionSpec],
    store_of: Callable[[str], ActivityStore],
) -> StoredDraft:
    """Takes back the draft's own log (B4, ticket #165): its source row goes, and so do the
    activities it brought, through its sport's `ActivityStore` (ADR-0032). When it was the
    session's last log, the session goes too, with its sources and a sport's own rows, and its
    other drafts' `session_id` is cleared by their own key (`SET NULL (session_id)`). The
    draft stays `confirmed` without a session. The draft row is locked first, then the
    session, as in a confirm, so a second undo waits for the first and then finds no
    session."""
    row = await _locked_draft(session, user_id, draft_id)
    if row.session_id is None:
        raise SessionNotFoundError(draft_id)
    training = await session.scalar(
        select(TrainingSessionOrm)
        .where(
            TrainingSessionOrm.id == row.session_id,
            TrainingSessionOrm.user_id == user_id,
        )
        .with_for_update()
    )
    assert training is not None  # its key clears `session_id` when the session goes
    log = (
        await session.execute(
            select(ChatMessageOrm.text, ChatMessageOrm.created_at).where(
                ChatMessageOrm.id == row.message_id, ChatMessageOrm.user_id == user_id
            )
        )
    ).one()
    source_id = await session.scalar(
        select(TrainingSessionSourceOrm.id)
        .where(
            TrainingSessionSourceOrm.session_id == training.id,
            TrainingSessionSourceOrm.user_id == user_id,
            TrainingSessionSourceOrm.raw_text == log.text,
            TrainingSessionSourceOrm.logged_at == log.created_at,
        )
        .limit(1)
    )
    assert source_id is not None  # a confirmed draft's log has its source row in its session
    left = await session.scalar(
        select(func.count()).where(
            TrainingSessionSourceOrm.session_id == training.id,
            TrainingSessionSourceOrm.user_id == user_id,
            TrainingSessionSourceOrm.id != source_id,
        )
    )
    if left:
        # The sport takes back only this log's activities, before its source row goes.
        training.payload = await store_of(row.sport_id).withdraw(
            session,
            user_id,
            LoggedActivities(
                session_id=training.id,
                source_id=source_id,
                session_payload=training.payload,
                addition=SessionDraft.model_validate(row.draft).payload,
            ),
        )
        training.payload_version = spec_of(row.sport_id).payload_version
        training.updated_at = undone_at
        await session.execute(
            delete(TrainingSessionSourceOrm).where(
                TrainingSessionSourceOrm.id == source_id,
                TrainingSessionSourceOrm.user_id == user_id,
            )
        )
        row.session_id = None
        await session.flush()
    else:
        # Its sources and a sport's own rows go with it, by their keys.
        await session.execute(
            delete(TrainingSessionOrm).where(
                TrainingSessionOrm.id == training.id,
                TrainingSessionOrm.user_id == user_id,
            )
        )
        await session.refresh(row)
    undone = await with_span(session, user_id, row)
    await session.commit()
    return undone
