"""The draft card in the chat (PRD-0004 F2, ADR-0022, ticket #154): a climbing log ends its
stream with a pending draft card under the reply's line, the card comes back on every page
load, an assumed date says so, and a failed extraction ends with the retryable error row."""

import re
from datetime import date, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape

from ai_trainer.application.chat import HISTORY_LIMIT
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction, ExtractedClimb
from ai_trainer.domain.sports.climbing.payload import RouteRef
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.web.drafts import draft_cards
from ai_trainer.web.icons import render_icon, vendored_icon_names
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    DRAFT_READY_TEXT,
    FROZEN_NOW,
    THREE_6A_TWO_6B,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_CLIMB = "I climbed 3 6A and 2 6B boulders"
_TIMES = "\N{MULTIPLICATION SIGN}"
_CLIMB_INTENTS = [[Intent(kind="log_session", sport="climbing")]]


def _client(
    conversation: ScriptedConversation | None = None,
) -> tuple[TestClient, UUID, InMemoryChatRepository, InMemoryDraftRepository]:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    chat = include_chat(
        app,
        build_templates(),
        conversation=conversation or ScriptedConversation(intents=_CLIMB_INTENTS),
        drafts=drafts,
    )
    user = sign_in_as(app)
    return TestClient(app), user.id, chat, drafts


async def _logged(chat: InMemoryChatRepository, user_id: UUID, text: str = _CLIMB) -> ChatMessage:
    return await chat.add(
        user_id, await chat.conversation_of(user_id), ChatRole.USER, text, FROZEN_NOW
    )


def _final(body: str) -> str:
    events = [block for block in body.replace("\r\n", "\n").split("\n\n") if block.strip()]
    return "\n".join(
        line.removeprefix("data: ") for line in events[-1].split("\n") if line.startswith("data:")
    )


def _cards(html: str) -> list[str]:
    return re.findall(r'<article[^>]*aria-label="Draft session".*?</article>', html, re.S)


async def test_a_climbing_log_ends_its_stream_with_a_draft_card_of_both_activities() -> None:
    client, user_id, chat, _ = _client()
    asked = await _logged(chat, user_id)

    final = _final(client.get(f"/messages/{asked.id}/reply").text)

    assert f'hx-target="#reply-{asked.id}"' in final
    assert str(escape(DRAFT_READY_TEXT)) in final
    [card] = _cards(final)
    assert "Climbing · draft" in card
    assert str(render_icon("mountain", "size-4")) in card
    rows = re.findall(r"<li.*?</li>", card, re.S)
    assert len(rows) == 2
    assert f"3 {_TIMES} 6A" in rows[0]
    assert f"2 {_TIMES} 6B" in rows[1]
    assert all("Boulder" in row for row in rows)


async def test_the_card_is_still_there_after_a_reload() -> None:
    client, user_id, chat, _ = _client()
    asked = await _logged(chat, user_id)
    client.get(f"/messages/{asked.id}/reply")

    for page in (client.get("/"), client.get(f"/c/{asked.conversation_id}")):
        [card] = _cards(page.text)
        assert f"3 {_TIMES} 6A" in card
        assert f"2 {_TIMES} 6B" in card
        assert "hx-sse:connect" not in page.text


async def test_an_assumed_date_today_says_so_on_the_chip() -> None:
    client, user_id, chat, _ = _client()
    asked = await _logged(chat, user_id)

    [card] = _cards(_final(client.get(f"/messages/{asked.id}/reply").text))

    assert 'data-date-source="assumed_today"' in card
    assert "Assumed: today, Fri 2 Oct" in card


async def test_a_stated_date_is_shown_plain_without_assumed() -> None:
    stated = THREE_6A_TWO_6B.model_copy(
        update={"occurred_on": date(2026, 9, 29), "date_source": "stated"}
    )
    client, user_id, chat, _ = _client(
        ScriptedConversation(intents=_CLIMB_INTENTS, extractions=[stated])
    )
    asked = await _logged(chat, user_id, "29.09: 3 6A and 2 6B boulders")

    [card] = _cards(_final(client.get(f"/messages/{asked.id}/reply").text))

    assert "Tue 29 Sep" in card
    assert "Assumed" not in card


async def test_an_assumed_date_seen_on_a_later_day_drops_today() -> None:
    yesterday = THREE_6A_TWO_6B.model_copy(update={"occurred_on": date(2026, 10, 1)})
    client, user_id, chat, _ = _client(
        ScriptedConversation(intents=_CLIMB_INTENTS, extractions=[yesterday])
    )
    asked = await _logged(chat, user_id)

    [card] = _cards(_final(client.get(f"/messages/{asked.id}/reply").text))

    assert "Assumed: Thu 1 Oct" in card
    assert "today," not in card


async def test_confirm_is_wired_and_edit_and_discard_stay_disabled_until_they_are() -> None:
    client, user_id, chat, _ = _client()
    asked = await _logged(chat, user_id)

    [card] = _cards(_final(client.get(f"/messages/{asked.id}/reply").text))

    buttons = re.findall(r"<button[^>]*>.*?</button>", card, re.S)
    assert [re.sub(r"<[^>]+>", "", button).strip() for button in buttons] == [
        "Confirm",
        "Edit",
        "Discard",
    ]
    confirm, edit, discard = (button.split(">")[0] for button in buttons)
    assert "hx-post" in confirm
    assert " disabled" not in confirm
    assert all(" disabled" in tag and "hx-post" not in tag for tag in (edit, discard))


async def test_an_extraction_failure_ends_with_the_retryable_error_row_and_no_card() -> None:
    client, user_id, chat, drafts = _client(
        ScriptedConversation(intents=_CLIMB_INTENTS, extract_outcomes=[LlmCallOutcome.ERROR])
    )
    asked = await _logged(chat, user_id)

    final = _final(client.get(f"/messages/{asked.id}/reply").text)
    page = client.get("/")

    assert "The reply didn't come through." in final
    assert f'hx-get="/messages/{asked.id}/retry"' in final
    assert _cards(final) == []
    assert _cards(page.text) == []
    assert drafts.all_of(user_id) == []


async def test_an_extraction_timeout_says_the_reply_took_too_long() -> None:
    client, user_id, chat, _ = _client(
        ScriptedConversation(intents=_CLIMB_INTENTS, extract_outcomes=[LlmCallOutcome.TIMEOUT])
    )
    asked = await _logged(chat, user_id)

    final = _final(client.get(f"/messages/{asked.id}/reply").text)

    assert "The reply took too long." in final
    assert _cards(final) == []


async def test_a_stop_that_arrives_after_the_draft_was_saved_shows_its_card() -> None:
    client, user_id, chat, _ = _client()
    asked = await _logged(chat, user_id)
    client.get(f"/messages/{asked.id}/reply")

    response = client.get(f"/messages/{asked.id}/stop", headers=_HTMX)

    assert len(_cards(response.text)) == 1
    assert "Stopped" not in response.text


async def test_a_replayed_stream_shows_the_same_card_with_no_model_call() -> None:
    conversation = ScriptedConversation(intents=_CLIMB_INTENTS)
    client, user_id, chat, drafts = _client(conversation)
    asked = await _logged(chat, user_id)
    client.get(f"/messages/{asked.id}/reply")

    final = _final(client.get(f"/messages/{asked.id}/reply").text)

    assert len(conversation.extracted) == 1
    assert len(_cards(final)) == 1
    assert len(drafts.all_of(user_id)) == 1


async def test_card_text_from_the_athlete_is_escaped() -> None:
    sneaky = ClimbingExtraction(
        occurred_on=date(2026, 10, 2),
        date_source="assumed_today",
        activities=[
            ExtractedClimb(
                kind="lead", count=1, grade="7a", route=RouteRef(name="<script>x</script>")
            )
        ],
    )
    client, user_id, chat, _ = _client(
        ScriptedConversation(intents=_CLIMB_INTENTS, extractions=[sneaky])
    )
    asked = await _logged(chat, user_id, "sent <script>x</script> 7a")

    [card] = _cards(_final(client.get(f"/messages/{asked.id}/reply").text))

    assert "<script>" not in card
    assert "&lt;script&gt;x&lt;/script&gt;" in card


def test_the_cards_icons_are_vendored() -> None:
    assert {"calendar", "check", "mountain"} <= vendored_icon_names()


def _stored(message_id: UUID, sport_id: str = "climbing") -> StoredDraft:
    return StoredDraft(
        id=uuid4(),
        conversation_id=uuid4(),
        message_id=message_id,
        sport_id=sport_id,
        draft=SessionDraft(
            occurred_on=date(2026, 10, 2),
            date_source="assumed_today",
            timezone="UTC",
            payload=THREE_6A_TWO_6B.to_payload().model_dump(mode="json"),
        ),
        payload_version=1,
        status="pending",
        created_at=FROZEN_NOW,
    )


def _message(
    role: ChatRole, *, sports: tuple[str, ...] = (), template: str | None = None
) -> ChatMessage:
    return ChatMessage(
        id=uuid4(),
        conversation_id=uuid4(),
        role=role,
        text="x",
        created_at=FROZEN_NOW,
        sports=sports,
        template_id=template,
        template_version=1 if template else None,
    )


def test_a_card_goes_under_its_logs_extraction_part_only() -> None:
    log = _message(ChatRole.USER)
    chitchat = _message(ChatRole.ASSISTANT, template="chitchat")
    extracted = _message(ChatRole.ASSISTANT, sports=("climbing",), template="climbing.extract")
    later_log = _message(ChatRole.USER)
    later_part = _message(ChatRole.ASSISTANT, sports=("climbing",), template="climbing.extract")
    draft = _stored(log.id)

    cards = draft_cards(
        [log, chitchat, extracted, later_log, later_part],
        [draft],
        sports=default_sport_registry(),
        today=date(2026, 10, 2),
    )

    assert list(cards) == [extracted.id]
    [card] = cards[extracted.id]
    assert (card.draft, card.is_today, card.body_template) == (
        draft,
        True,
        "partials/chat/draft_climbing.html",
    )


def test_a_draft_of_an_unknown_sport_or_under_a_non_extraction_part_shows_no_card() -> None:
    log = _message(ChatRole.USER)
    not_yet = _message(ChatRole.ASSISTANT, sports=("climbing", "rowing"), template="not_yet")

    cards = draft_cards(
        [log, not_yet],
        [_stored(log.id), _stored(log.id, sport_id="rowing")],
        sports=default_sport_registry(),
        today=date(2026, 10, 2),
    )

    assert cards == {}


def test_a_reply_without_its_log_in_the_list_takes_the_log_id_given() -> None:
    log_id = uuid4()
    part = _message(ChatRole.ASSISTANT, sports=("climbing",), template="climbing.extract")

    with_log = draft_cards(
        [part], [_stored(log_id)], sports=default_sport_registry(), today=date(2026, 10, 3)
    )
    without = draft_cards(
        [part],
        [_stored(log_id)],
        sports=default_sport_registry(),
        today=date(2026, 10, 3),
        log_id=log_id,
    )

    assert with_log == {}
    [card] = without[part.id]
    assert card.is_today is False


def test_a_draft_whose_payload_no_longer_reads_shows_no_card_rather_than_failing() -> None:
    log = _message(ChatRole.USER)
    part = _message(ChatRole.ASSISTANT, sports=("climbing",), template="climbing.extract")
    broken = _stored(log.id).model_copy(
        update={"draft": _stored(log.id).draft.model_copy(update={"payload": {"activities": "x"}})}
    )

    cards = draft_cards(
        [log, part], [broken], sports=default_sport_registry(), today=date(2026, 10, 2)
    )

    assert cards == {}


async def test_the_card_survives_a_reload_when_its_log_fell_out_of_the_history_window() -> None:
    client, user_id, chat, _ = _client()
    asked = await _logged(chat, user_id)
    client.get(f"/messages/{asked.id}/reply")
    conversation_id = asked.conversation_id
    # Enough later turns that the page's window starts right at the log's reply.
    for minute in range(1, HISTORY_LIMIT):
        role = ChatRole.USER if minute % 2 else ChatRole.ASSISTANT
        await chat.add(
            user_id, conversation_id, role, f"turn {minute}", FROZEN_NOW + timedelta(minutes=minute)
        )

    page = client.get(f"/c/{conversation_id}")

    assert _CLIMB not in page.text
    assert len(_cards(page.text)) == 1


async def test_a_window_opening_on_a_reply_to_an_assistant_turn_finds_no_log() -> None:
    client, user_id, chat, _ = _client()
    conversation_id = await chat.conversation_of(user_id)
    for minute in range(HISTORY_LIMIT + 1):
        await chat.add(
            user_id,
            conversation_id,
            ChatRole.ASSISTANT,
            f"turn {minute}",
            FROZEN_NOW + timedelta(minutes=minute),
        )

    page = client.get(f"/c/{conversation_id}")

    assert page.status_code == 200
    assert _cards(page.text) == []


def test_a_draft_skips_a_non_extraction_part_and_goes_under_its_extraction_part() -> None:
    log = _message(ChatRole.USER)
    not_yet = _message(ChatRole.ASSISTANT, sports=("climbing",), template="not_yet")
    extracted = _message(ChatRole.ASSISTANT, sports=("climbing",), template="climbing.extract")

    cards = draft_cards(
        [log, not_yet, extracted],
        [_stored(log.id)],
        sports=default_sport_registry(),
        today=date(2026, 10, 2),
    )

    assert list(cards) == [extracted.id]


def test_every_sport_that_can_be_logged_ships_its_card_body() -> None:
    templates = build_templates()
    for plugin in default_sport_registry().all():
        if plugin.extraction is not None:
            templates.get_template(f"partials/chat/draft_{plugin.id}.html")
