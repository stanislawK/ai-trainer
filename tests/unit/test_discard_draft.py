"""`discard_draft` (PRD-0004 F2, ADR-0022, ticket #156): the discard happens at the clock's time,
for the athlete's own pending draft only, and it never saves a session."""

from uuid import uuid4

import pytest

from ai_trainer.application.drafts import discard_draft
from ai_trainer.domain.training import DraftConfirmedError, DraftNotFoundError
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryDraftRepository, pending_draft


async def test_discard_marks_the_draft_discarded_at_the_clocks_time_and_saves_no_session() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    discarded = await discard_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert (discarded.id, discarded.status, discarded.session_id) == (draft.id, "discarded", None)
    assert drafts.discarded_at == {draft.id: FROZEN_NOW}
    assert drafts.confirmed_at == {}


async def test_discarding_twice_returns_the_same_discarded_draft() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    first = await discard_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())
    second = await discard_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert second == first


async def test_another_users_draft_is_not_found_and_stays_pending() -> None:
    drafts = InMemoryDraftRepository()
    owner = uuid4()
    draft = await pending_draft(drafts, owner)

    with pytest.raises(DraftNotFoundError):
        await discard_draft(uuid4(), draft.id, drafts=drafts, clock=FixedClock())

    assert [d.status for d in drafts.all_of(owner)] == ["pending"]
    assert drafts.discarded_at == {}


async def test_a_confirmed_draft_is_refused_and_stays_confirmed() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="confirmed")

    with pytest.raises(DraftConfirmedError):
        await discard_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert [d.status for d in drafts.all_of(user_id)] == ["confirmed"]
    assert drafts.discarded_at == {}
