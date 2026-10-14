"""`undo_log` (PRD-0004 B4, ADR-0022, tickets #157 and #165): the athlete's own confirmed
draft's log is taken back from its session, now, and the draft stays confirmed without it;
another user's draft is not found, and one without a session has nothing to undo."""

from uuid import uuid4

import pytest

from ai_trainer.application.drafts import undo_log
from ai_trainer.domain.training import DraftNotFoundError, SessionNotFoundError
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryDraftRepository, pending_draft


async def test_undo_takes_the_log_back_now_and_keeps_the_draft_confirmed_without_it() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="confirmed")
    assert draft.session_id is not None

    undone = await undo_log(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert (undone.id, undone.status, undone.session_id) == (draft.id, "confirmed", None)
    assert drafts.removed_sessions == [draft.session_id]
    assert drafts.undone_at == {draft.id: FROZEN_NOW}


async def test_another_users_draft_is_not_found_and_stays() -> None:
    drafts = InMemoryDraftRepository()
    owner = uuid4()
    draft = await pending_draft(drafts, owner, status="confirmed")

    with pytest.raises(DraftNotFoundError):
        await undo_log(uuid4(), draft.id, drafts=drafts, clock=FixedClock())

    assert [d.session_id for d in drafts.all_of(owner)] == [draft.session_id]
    assert drafts.removed_sessions == []


async def test_undoing_twice_finds_no_session_the_second_time() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="confirmed")
    await undo_log(user_id, draft.id, drafts=drafts, clock=FixedClock())

    with pytest.raises(SessionNotFoundError):
        await undo_log(user_id, draft.id, drafts=drafts, clock=FixedClock())
