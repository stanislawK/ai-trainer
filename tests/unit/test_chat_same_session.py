"""The same-or-new question on the draft card (PRD-0004 B16, B17, F9, ADR-0014, ADR-0015,
ticket #166): a log three hours after a same-day session streams a draft card whose note line
is a question with two choice rows, "Add to the session at HH:MM" and "New session", and whose
Confirm waits for the pick. A tap posts the option's ID and swaps in the settled card. The mocks
have no frame for it; the layout inside the card was owner-chosen at the plan gate on
2026-10-21."""

import re
from datetime import date, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import NEW_SESSION_OPTION
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.training import SessionSpan
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    SAME_SESSION_QUESTION_TEXT,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    InMemoryTrainingSessions,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
# The morning's session: its first log at 06:00 UTC, the athlete's zone here, its last three
# hours before `FROZEN_NOW`.
_MORNING = SessionSpan(
    id=uuid4(),
    occurred_on=date(2026, 10, 2),
    first_logged_at=FROZEN_NOW - timedelta(hours=3, minutes=30),
    last_logged_at=FROZEN_NOW - timedelta(hours=3),
)


class _App:
    def __init__(self) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.sessions = InMemoryTrainingSessions()
        self.profile = InMemoryProfile()
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=ScriptedConversation(
                intents=[[Intent(kind="log_session", sport="climbing")]]
            ),
            profile=self.profile,
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=self.sessions,
        )
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = ["climbing"]
        self.sessions.latest[(self.user.id, "climbing")] = _MORNING
        self.drafts.spans[_MORNING.id] = _MORNING
        self.client = TestClient(app)

    async def logged(self) -> tuple[ChatMessage, str]:
        message = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            "and 2 6B",
            FROZEN_NOW,
        )
        return message, self.client.get(f"/messages/{message.id}/reply").text

    async def card(self) -> tuple[UUID, UUID, str]:
        """The pending draft, the question on its card and the stream that showed it."""
        _, body = await self.logged()
        [draft] = self.drafts.all_of(self.user.id)
        [question] = self.clarifications.all_of(self.user.id)
        return draft.id, question.id, body

    def answer(self, clarification_id: UUID, *option_ids: str):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"/clarifications/{clarification_id}/answer",
            data={"option_ids": list(option_ids)},
            headers=_HTMX,
        )


def _flat(html: str) -> str:
    return " ".join(html.split())


def _confirm(html: str) -> str:
    [button] = re.findall(r"<button[^>]*/confirm\"[^>]*>", html)
    return str(button)


async def test_the_card_asks_same_or_new_with_the_sessions_time_and_waits_for_the_pick() -> None:
    app = _App()

    draft_id, question_id, body = await app.card()

    html = _flat(body)
    assert SAME_SESSION_QUESTION_TEXT in html
    assert html.count("data-choice-option=") == 2
    assert f"""hx-vals='{{"option_ids": "{_MORNING.id}"}}'""" in html
    assert f"""hx-vals='{{"option_ids": "{NEW_SESSION_OPTION}"}}'""" in html
    assert "Add to the session at 06:00" in html
    assert "New session" in html
    assert f'hx-post="/clarifications/{question_id}/answer"' in html
    assert "Let me type instead" not in html
    assert "data-adds-to-session" not in html
    assert " disabled" in _confirm(html)
    assert html.index("data-choice-option=") < html.index(f"/drafts/{draft_id}/confirm")


async def test_the_question_stays_on_the_card_after_a_reload() -> None:
    app = _App()
    _, _, _ = await app.card()

    page = _flat(app.client.get("/").text)

    assert SAME_SESSION_QUESTION_TEXT in page
    assert "Add to the session at 06:00" in page
    assert " disabled" in _confirm(page)


async def test_picking_add_swaps_in_the_card_that_adds_to_the_session() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()

    response = app.answer(question_id, str(_MORNING.id))

    assert response.status_code == 200
    html = _flat(response.text)
    assert f'data-draft-id="{draft_id}"' in html
    assert "data-choice-option" not in html
    assert re.search(r"data-adds-to-session[^>]*>.*Add to the session at 06:00", html)
    assert " disabled" not in _confirm(html)
    assert app.drafts.extends_ids[draft_id] == _MORNING.id


async def test_picking_new_swaps_in_a_plain_card() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()

    response = app.answer(question_id, NEW_SESSION_OPTION)

    assert response.status_code == 200
    html = _flat(response.text)
    assert "data-choice-option" not in html
    assert "Add to the session" not in html
    assert " disabled" not in _confirm(html)
    assert app.drafts.extends_ids[draft_id] is None


async def test_confirm_before_the_pick_is_a_409_and_saves_nothing() -> None:
    app = _App()
    draft_id, _, _ = await app.card()

    response = app.client.post(f"/drafts/{draft_id}/confirm", headers=_HTMX)

    assert response.status_code == 409
    assert app.drafts.confirmed_at == {}


async def test_an_option_the_question_never_offered_is_a_422() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()

    response = app.answer(question_id, "Add to the session at 06:00")

    assert response.status_code == 422
    assert app.drafts.extends_ids[draft_id] is None


async def test_a_second_answer_is_a_409() -> None:
    app = _App()
    _, question_id, _ = await app.card()
    app.answer(question_id, NEW_SESSION_OPTION)

    assert app.answer(question_id, str(_MORNING.id)).status_code == 409


async def test_answering_after_a_discard_is_a_409() -> None:
    app = _App()
    draft_id, question_id, _ = await app.card()
    app.client.post(f"/drafts/{draft_id}/discard", headers=_HTMX)

    assert app.answer(question_id, NEW_SESSION_OPTION).status_code == 409


async def test_a_discarded_draft_shows_no_question() -> None:
    app = _App()
    draft_id, _, _ = await app.card()
    app.client.post(f"/drafts/{draft_id}/discard", headers=_HTMX)

    page = _flat(app.client.get("/").text)

    assert "Draft discarded" in page
    assert SAME_SESSION_QUESTION_TEXT not in page


async def test_reopening_the_card_from_its_edit_form_keeps_the_question() -> None:
    app = _App()
    draft_id, _, _ = await app.card()

    html = _flat(app.client.get(f"/drafts/{draft_id}", headers=_HTMX).text)

    assert SAME_SESSION_QUESTION_TEXT in html
    assert " disabled" in _confirm(html)


async def test_skipping_the_question_is_not_offered_and_a_posted_skip_is_a_404() -> None:
    app = _App()
    _, question_id, _ = await app.card()

    response = app.client.post(f"/clarifications/{question_id}/abandon", headers=_HTMX)

    assert response.status_code == 404
    [still] = app.clarifications.all_of(app.user.id)
    assert still.status == "open"


async def test_a_tap_disables_only_buttons_that_are_enabled_so_a_failed_one_keeps_confirm_off() -> (
    None
):
    """htmx 4 re-enables every element `hx-disable` disabled when the request ends, so the rows
    must leave the already disabled Confirm out of it."""
    app = _App()
    draft_id, _, body = await app.card()

    selectors = set(re.findall(r'hx-disable="([^"]*)"[^>]*data-choice-option', body))

    assert selectors == {f"[data-draft-id=&#39;{draft_id}&#39;] button:not([disabled])"}
