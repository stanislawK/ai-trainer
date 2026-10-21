from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import (
    ChatMessageOrm,
    SessionDraftOrm,
    TrainingSessionOrm,
    TrainingSessionSourceOrm,
)
from ai_trainer.adapters.training_session_repository import spans_of
from ai_trainer.domain.sports.base import ExtractionSpec, extend_payload, withdraw_payload
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.domain.training import (
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

_PENDING: DraftStatus = "pending"
_CONFIRMED: DraftStatus = "confirmed"
_DISCARDED: DraftStatus = "discarded"
_CHAT_SOURCE = "chat"
# The envelope fields an addition to a session fills when the session has none (ticket #165).
_FILLED_FIELDS = ("started_at", "duration_min", "rpe", "feeling", "notes")


def _to_domain(row: SessionDraftOrm, spans: Mapping[UUID, SessionSpan]) -> StoredDraft:
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
            "created_at": row.created_at,
        }
    )


async def _with_span(session: AsyncSession, user_id: UUID, row: SessionDraftOrm) -> StoredDraft:
    ids = [] if row.extends_session_id is None else [row.extends_session_id]
    return _to_domain(row, await spans_of(session, user_id, ids))


class SqlAlchemySessionDraftRepository:
    """Implements `SessionDraftRepositoryPort` (ADR-0004, ADR-0022). The conversation key is
    composite with `user_id`, so a draft can't join another user's conversation, nor extend
    another user's session. An addition to a session goes through its sport's payload model
    (ADR-0022), so the repository knows the sports."""

    def __init__(
        self, session_factory: Callable[[], AsyncSession], sports: SportRegistry | None = None
    ) -> None:
        self._session_factory = session_factory
        self._sports = sports or default_sport_registry()

    def _spec(self, sport_id: str) -> ExtractionSpec:
        spec = self._sports.get(sport_id).extraction
        assert spec is not None  # a draft is only ever made by its sport's extraction
        return spec

    async def add(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        sport_id: str,
        draft: SessionDraft,
        *,
        payload_version: int,
        created_at: datetime,
        extends_session_id: UUID | None = None,
    ) -> StoredDraft:
        async with self._session_factory() as session:
            row = SessionDraftOrm(
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                sport_id=sport_id,
                draft=draft.model_dump(mode="json"),
                payload_version=payload_version,
                status=_PENDING,
                extends_session_id=extends_session_id,
                created_at=created_at,
            )
            session.add(row)
            # Flushed, not read after the commit: a committed row's attributes expire.
            await session.flush()
            stored = await _with_span(session, user_id, row)
            await session.commit()
            return stored

    async def list_shown(self, user_id: UUID, conversation_id: UUID) -> Sequence[StoredDraft]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(SessionDraftOrm)
                .where(
                    SessionDraftOrm.user_id == user_id,
                    SessionDraftOrm.conversation_id == conversation_id,
                )
                .order_by(SessionDraftOrm.created_at, SessionDraftOrm.id)
            )
            shown = rows.all()
            extended = {row.extends_session_id for row in shown} - {None}
            spans = await spans_of(session, user_id, [id_ for id_ in extended if id_ is not None])
            return [_to_domain(row, spans) for row in shown]

    async def get(self, user_id: UUID, draft_id: UUID) -> StoredDraft:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm).where(
                    SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id
                )
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            return await _with_span(session, user_id, row)

    async def update(
        self, user_id: UUID, draft_id: UUID, draft: SessionDraft, *, payload_version: int
    ) -> StoredDraft:
        """The row is locked first, as in a confirm, so an edit racing a confirm or a discard
        either goes first, and the confirm saves the edit, or finds the draft resolved and is
        refused."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == _CONFIRMED:
                raise DraftConfirmedError(draft_id)
            if row.status == _DISCARDED:
                raise DraftDiscardedError(draft_id)
            row.draft = draft.model_dump(mode="json")
            row.payload_version = payload_version
            await session.flush()
            edited = await _with_span(session, user_id, row)
            await session.commit()
            return edited

    async def change_sport(
        self,
        user_id: UUID,
        draft_id: UUID,
        sport_id: str,
        draft: SessionDraft,
        *,
        payload_version: int,
        extends_session_id: UUID | None,
    ) -> StoredDraft:
        """Locked first, as an edit is, so a switch racing a confirm or a discard either goes
        first, and the confirm saves the new sport, or finds the draft resolved and is
        refused."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == _CONFIRMED:
                raise DraftConfirmedError(draft_id)
            if row.status == _DISCARDED:
                raise DraftDiscardedError(draft_id)
            row.sport_id = sport_id
            row.draft = draft.model_dump(mode="json")
            row.payload_version = payload_version
            row.extends_session_id = extends_session_id
            await session.flush()
            changed = await _with_span(session, user_id, row)
            await session.commit()
            return changed

    async def confirm(
        self, user_id: UUID, draft_id: UUID, *, confirmed_at: datetime
    ) -> StoredDraft:
        """One transaction (ADR-0022): the session, or the addition to the one the draft
        extends (B16), its source row with the log's original text (B2), and the draft's move to
        `confirmed`. The draft row is locked first, so a second confirm waits for the first and
        then finds it done: one session either way. The extended session is locked next, as in
        an undo, so two additions to it land one after the other."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == _DISCARDED:
                raise DraftDiscardedError(draft_id)
            if row.status == _CONFIRMED:
                return await _with_span(session, user_id, row)
            log = (
                await session.execute(
                    select(ChatMessageOrm.text, ChatMessageOrm.created_at).where(
                        ChatMessageOrm.id == row.message_id, ChatMessageOrm.user_id == user_id
                    )
                )
            ).one()
            draft = SessionDraft.model_validate(row.draft)
            training = await self._extended(session, user_id, row, draft)
            if training is None:
                # The session is gone, or the draft now names another day: a session of its own.
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
                    payload=draft.payload,
                    payload_version=row.payload_version,
                    created_at=confirmed_at,
                    updated_at=confirmed_at,
                )
                session.add(training)
                await session.flush()
            else:
                spec = self._spec(row.sport_id)
                training.payload = extend_payload(
                    spec.payload_type, training.payload, draft.payload
                ).model_dump(mode="json")
                training.payload_version = spec.payload_version
                for name in _FILLED_FIELDS:
                    if getattr(training, name) is None:
                        setattr(training, name, getattr(draft, name))
                training.updated_at = confirmed_at
            session.add(
                TrainingSessionSourceOrm(
                    user_id=user_id,
                    session_id=training.id,
                    raw_text=log.text,
                    source=_CHAT_SOURCE,
                    logged_at=log.created_at,
                )
            )
            row.status = _CONFIRMED
            row.session_id = training.id
            row.resolved_at = confirmed_at
            await session.flush()
            confirmed = await _with_span(session, user_id, row)
            await session.commit()
            return confirmed

    @staticmethod
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

    async def discard(
        self, user_id: UUID, draft_id: UUID, *, discarded_at: datetime
    ) -> StoredDraft:
        """Marks the draft `discarded`; it writes no session (ADR-0022 invariant 3). The row is
        locked first, so a discard racing a confirm either finds it confirmed and is refused,
        or goes first and makes the confirm refuse."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
            if row.status == _CONFIRMED:
                raise DraftConfirmedError(draft_id)
            if row.status == _PENDING:
                row.status = _DISCARDED
                row.resolved_at = discarded_at
            await session.flush()
            discarded = await _with_span(session, user_id, row)
            await session.commit()
            return discarded

    async def undo(self, user_id: UUID, draft_id: UUID, *, undone_at: datetime) -> StoredDraft:
        """Takes back the draft's own log (B4, ticket #165): its source row goes, and so do the
        activities it brought, through its sport's payload model. When it was the session's
        last log, the session goes too, and its other drafts' `session_id` is cleared by their
        own key (`SET NULL (session_id)`). The draft stays `confirmed` without a session. The
        draft row is locked first, then the session, as in a confirm, so a second undo waits for
        the first and then finds no session."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SessionDraftOrm)
                .where(SessionDraftOrm.id == draft_id, SessionDraftOrm.user_id == user_id)
                .with_for_update()
            )
            if row is None:
                raise DraftNotFoundError(draft_id)
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
            await session.execute(
                delete(TrainingSessionSourceOrm).where(
                    TrainingSessionSourceOrm.id == source_id,
                    TrainingSessionSourceOrm.user_id == user_id,
                )
            )
            left = await session.scalar(
                select(func.count()).where(
                    TrainingSessionSourceOrm.session_id == training.id,
                    TrainingSessionSourceOrm.user_id == user_id,
                )
            )
            if left:
                spec = self._spec(row.sport_id)
                draft = SessionDraft.model_validate(row.draft)
                training.payload = withdraw_payload(
                    spec.payload_type, training.payload, draft.payload
                ).model_dump(mode="json")
                training.payload_version = spec.payload_version
                training.updated_at = undone_at
                row.session_id = None
                await session.flush()
            else:
                await session.execute(
                    delete(TrainingSessionOrm).where(
                        TrainingSessionOrm.id == training.id,
                        TrainingSessionOrm.user_id == user_id,
                    )
                )
                await session.refresh(row)
            undone = await _with_span(session, user_id, row)
            await session.commit()
            return undone
