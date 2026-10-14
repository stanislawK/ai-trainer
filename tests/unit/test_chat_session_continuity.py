"""The draft card's offer to add a log to the session it came close after (PRD-0004 B16,
ADR-0014, ticket #165): the pending card says "Add to the session at HH:MM", in the athlete's
own time, and the confirmed row says what was added to it. The Chat mock has no frame for it; a
note line above the actions was owner-approved at the plan gate on 2026-10-13."""

import re
from datetime import date, timedelta
from uuid import uuid4

from ai_trainer.domain.training import SessionDraft, SessionSpan, StoredDraft
from ai_trainer.web.drafts import DraftCard, draft_card
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryDraftRepository,
    pending_draft,
    sports_with_rowing,
)

# 09:10 UTC is 11:10 in Warsaw, where the draft was resolved (CEST, UTC+2).
_SESSION = SessionSpan(
    id=uuid4(),
    occurred_on=date(2026, 10, 2),
    first_logged_at=FROZEN_NOW - timedelta(minutes=20),
    last_logged_at=FROZEN_NOW - timedelta(minutes=5),
)


async def _extending(**changes: object) -> StoredDraft:
    draft = await pending_draft(InMemoryDraftRepository(), uuid4())
    return draft.model_copy(update={"extends": _SESSION, **changes})


def _card(draft: StoredDraft) -> DraftCard:
    card = draft_card(draft, sports=sports_with_rowing(), today=date(2026, 10, 2))
    assert card is not None
    return card


def _render(card: DraftCard) -> str:
    template = build_templates().env.get_template(card.template)
    return " ".join(template.render(card=card, draft=card.draft).split())


async def test_a_pending_draft_extending_a_session_offers_to_add_to_it_at_its_local_time() -> None:
    card = _card(await _extending())

    html = _render(card)

    assert card.adds_at == "11:10"
    (note,) = re.findall(r"<p[^>]*data-adds-to-session[^>]*>.*?</p>", html)
    assert "Add to the session at 11:10" in note
    assert "M12.83 2.18" in note  # the vendored `layers` icon
    assert html.index("data-adds-to-session") < html.index("/confirm")
    assert ">Confirm</button>" in html.replace(" </button>", "</button>")


async def test_a_pending_draft_extending_nothing_makes_no_offer() -> None:
    draft = await pending_draft(InMemoryDraftRepository(), uuid4())

    card = _card(draft)

    assert card.adds_at is None
    assert "Add to the session" not in _render(card)


async def test_a_draft_edited_to_another_day_no_longer_offers_to_add() -> None:
    draft = await _extending()
    yesterday = draft.draft.model_copy(
        update={"occurred_on": date(2026, 10, 1), "date_source": "stated"}
    )

    card = _card(draft.model_copy(update={"draft": yesterday}))

    assert card.adds_at is None
    assert "Add to the session" not in _render(card)


async def test_the_offer_reads_the_time_in_the_drafts_own_timezone() -> None:
    draft = await _extending()
    in_new_york = SessionDraft.model_validate(
        {**draft.draft.model_dump(), "timezone": "America/New_York"}
    )

    card = _card(draft.model_copy(update={"draft": in_new_york}))

    assert card.adds_at == "05:10"


async def test_a_confirmed_addition_says_what_was_added_to_the_session() -> None:
    card = _card(await _extending(status="confirmed", session_id=_SESSION.id))

    html = _render(card)

    assert card.template == "partials/chat/draft_confirmed.html"
    assert re.search(r"Added .*? to the session at 11:10", html)
    assert "Logged" not in html
    assert "Undo" in html


async def test_a_confirmed_draft_that_saved_a_session_of_its_own_says_logged() -> None:
    card = _card(await _extending(status="confirmed", session_id=uuid4(), extends=None))

    html = _render(card)

    assert "Logged" in html
    assert "to the session at" not in html
