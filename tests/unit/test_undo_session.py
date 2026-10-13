"""`undo_session` (PRD-0004 B4, ADR-0022, ticket #157): the athlete's own just-saved session is
removed and its draft stays confirmed without it; another user's session is not found."""

from uuid import uuid4

import pytest

from ai_trainer.application.drafts import undo_session
from ai_trainer.domain.training import SessionNotFoundError
from tests.chat_support import InMemoryDraftRepository, pending_draft


async def test_undo_removes_the_session_and_keeps_the_draft_confirmed_without_it() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="confirmed")
    assert draft.session_id is not None

    undone = await undo_session(user_id, draft.session_id, drafts=drafts)

    assert (undone.id, undone.status, undone.session_id) == (draft.id, "confirmed", None)
    assert drafts.removed_sessions == [draft.session_id]


async def test_another_users_session_is_not_found_and_stays() -> None:
    drafts = InMemoryDraftRepository()
    owner = uuid4()
    draft = await pending_draft(drafts, owner, status="confirmed")

    with pytest.raises(SessionNotFoundError):
        await undo_session(uuid4(), draft.session_id or uuid4(), drafts=drafts)

    assert [d.session_id for d in drafts.all_of(owner)] == [draft.session_id]
    assert drafts.removed_sessions == []


async def test_undoing_twice_is_not_found_the_second_time() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="confirmed")
    assert draft.session_id is not None
    await undo_session(user_id, draft.session_id, drafts=drafts)

    with pytest.raises(SessionNotFoundError):
        await undo_session(user_id, draft.session_id, drafts=drafts)
