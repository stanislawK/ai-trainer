"""Answering a session draft (PRD-0004 F2, ADR-0022). Only the athlete's confirm writes a
training session (ADR-0022 invariant 3)."""

from uuid import UUID

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.drafts import SessionDraftRepositoryPort
from ai_trainer.domain.training import StoredDraft


async def confirm_draft(
    user_id: UUID, draft_id: UUID, *, drafts: SessionDraftRepositoryPort, clock: ClockPort
) -> StoredDraft:
    """Saves the athlete's own draft as a training session, now. Confirming it again returns the
    same session; another user's draft is not found, and a discarded one is refused."""
    return await drafts.confirm(user_id, draft_id, confirmed_at=clock.now())


async def discard_draft(
    user_id: UUID, draft_id: UUID, *, drafts: SessionDraftRepositoryPort, clock: ClockPort
) -> StoredDraft:
    """Throws away the athlete's own draft, now, without saving a session. Discarding it again
    returns the same draft; another user's draft is not found, and a confirmed one is refused."""
    return await drafts.discard(user_id, draft_id, discarded_at=clock.now())
