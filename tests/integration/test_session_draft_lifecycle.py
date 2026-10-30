"""The confirm path's single transaction (ADR-0022, ticket #346): a confirm that fails after the
session insert rolls back whole."""

from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters import session_draft_lifecycle
from tests.integration.test_session_draft_repository import _START, _counts, _pending


async def test_a_confirm_failing_after_the_session_insert_leaves_no_session_and_the_draft_shown(
    db_session_factory: Callable[[], AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts, user_id, draft = await _pending(db_session_factory, "confirm-rollback")

    async def fail(*_: object) -> None:
        raise RuntimeError("the confirm fails after the session and its source are flushed")

    with monkeypatch.context() as patch:
        patch.setattr(session_draft_lifecycle, "with_span", fail)
        with pytest.raises(RuntimeError, match="after the session"):
            await drafts.confirm(user_id, draft.id, confirmed_at=_START)

    assert await _counts(db_session_factory, user_id) == (0, 0)
    assert await drafts.list_shown(user_id, draft.conversation_id) == [draft]
    assert (draft.status, draft.session_id) == ("pending", None)
