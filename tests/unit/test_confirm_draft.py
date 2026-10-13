"""`confirm_draft` (PRD-0004 F2, ADR-0022, ticket #155): the confirm happens at the clock's time,
for the athlete's own draft only."""

from uuid import uuid4

import pytest

from ai_trainer.application.drafts import confirm_draft
from ai_trainer.domain.training import DraftDiscardedError, DraftNotFoundError
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryDraftRepository, pending_draft


async def test_confirm_marks_the_draft_confirmed_at_the_clocks_time() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    confirmed = await confirm_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert (confirmed.id, confirmed.status) == (draft.id, "confirmed")
    assert confirmed.session_id is not None
    assert drafts.confirmed_at == {draft.id: FROZEN_NOW}


async def test_confirming_twice_returns_the_same_session() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    first = await confirm_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())
    second = await confirm_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert second == first


async def test_another_users_draft_is_not_found() -> None:
    drafts = InMemoryDraftRepository()
    draft = await pending_draft(drafts, uuid4())

    with pytest.raises(DraftNotFoundError):
        await confirm_draft(uuid4(), draft.id, drafts=drafts, clock=FixedClock())

    assert drafts.confirmed_at == {}


async def test_a_discarded_draft_is_refused() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="discarded")

    with pytest.raises(DraftDiscardedError):
        await confirm_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert drafts.confirmed_at == {}
