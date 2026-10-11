"""The streamed reply (PRD-0003 F1, ADR-0008, ADR-0012, ticket #82): `POST /messages` appends a
placeholder that connects to `GET /messages/{id}/reply`, which streams the reply as htmx
partials and ends by swapping in the saved reply, or a retryable error row."""

import re
from html.parser import HTMLParser
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    NOT_YET_TEXT,
    InMemoryChatRepository,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}


def _client(
    conversation: ScriptedConversation | None = None,
    chat: InMemoryChatRepository | None = None,
) -> tuple[TestClient, UUID, InMemoryChatRepository]:
    app = FastAPI()
    repository = include_chat(app, build_templates(), chat, conversation=conversation)
    user = sign_in_as(app)
    return TestClient(app), user.id, repository


def _sse_data(body: str) -> list[str]:
    """Each event's data, its `data:` lines joined back with newlines as a browser does."""
    events = [block for block in body.replace("\r\n", "\n").split("\n\n") if block.strip()]
    return [
        "\n".join(
            line.removeprefix("data: ") for line in event.split("\n") if line.startswith("data:")
        )
        for event in events
    ]


async def _asked(chat: InMemoryChatRepository, user_id: UUID, text: str = "hi!") -> ChatMessage:
    return await chat.add(user_id, ChatRole.USER, text, FROZEN_NOW)


def test_a_sent_message_is_followed_by_a_reply_placeholder_that_connects_once() -> None:
    client, _, chat = _client()

    response = client.post("/messages", data={"message": "hi!"}, headers=_HTMX)

    [(_, asked)] = chat.rows
    assert f'id="reply-{asked.id}"' in response.text
    assert f'hx-sse:connect="/messages/{asked.id}/reply"' in response.text
    assert 'hx-config="sse.reconnect:false sse.pauseOnBackground:false"' in response.text


async def test_hi_streams_chunks_then_swaps_in_the_saved_reply() -> None:
    client, user_id, chat = _client(ScriptedConversation(intents=[[Intent(kind="chitchat")]]))
    asked = await _asked(chat, user_id)

    response = client.get(f"/messages/{asked.id}/reply")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    _routing, _replying, *chunks, final = _sse_data(response.text)
    for chunk, text in zip(chunks, ["Hey! ", "Good to hear from you."], strict=True):
        assert f'hx-target="#reply-{asked.id}-text"' in chunk
        assert 'hx-swap="beforeend"' in chunk
        assert text in chunk
    assert f'hx-target="#reply-{asked.id}"' in final
    assert 'hx-swap="innerHTML"' in final
    assert "Hey! Good to hear from you." in final
    assert "hx-sse:connect" not in final


async def test_a_streamed_reply_survives_a_reload() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    client.get(f"/messages/{asked.id}/reply")
    page = client.get("/")

    assert "Hey! Good to hear from you." in page.text
    assert "hx-sse:connect" not in page.text


async def test_a_reload_before_the_reply_came_through_reconnects_for_it() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    page = client.get("/")

    assert f'hx-sse:connect="/messages/{asked.id}/reply"' in page.text


async def test_log_my_ride_streams_the_not_yet_reply() -> None:
    conversation = ScriptedConversation(intents=[[Intent(kind="log_session", sport="cycling")]])
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id, "log my ride")

    response = client.get(f"/messages/{asked.id}/reply")

    assert NOT_YET_TEXT.replace("'", "&#39;") in _sse_data(response.text)[-1]
    assert conversation.chitchatted == []


async def test_streamed_text_is_escaped() -> None:
    conversation = ScriptedConversation(chunks=["<b>bold</b>"])
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)

    response = client.get(f"/messages/{asked.id}/reply")

    assert "<b>bold</b>" not in response.text
    assert "&lt;b&gt;bold&lt;/b&gt;" in response.text


async def test_a_timeout_ends_the_stream_with_a_retryable_error_row() -> None:
    conversation = ScriptedConversation(chitchat_outcomes=[LlmCallOutcome.TIMEOUT])
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)

    response = client.get(f"/messages/{asked.id}/reply")

    final = _sse_data(response.text)[-1]
    assert f'hx-target="#reply-{asked.id}"' in final
    assert 'hx-swap="innerHTML"' in final
    assert "took too long" in final
    assert f'hx-get="/messages/{asked.id}/retry"' in final
    assert [message for _, message in chat.rows] == [asked]


async def test_an_error_ends_the_stream_with_a_retryable_error_row() -> None:
    conversation = ScriptedConversation(route_outcomes=[LlmCallOutcome.ERROR])
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)

    final = _sse_data(client.get(f"/messages/{asked.id}/reply").text)[-1]

    assert "didn't come through" in final
    assert f'hx-get="/messages/{asked.id}/retry"' in final


async def test_retry_swaps_the_error_row_for_a_fresh_placeholder_and_the_stream_replies() -> None:
    conversation = ScriptedConversation(
        chitchat_outcomes=[LlmCallOutcome.TIMEOUT, LlmCallOutcome.SUCCESS]
    )
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)
    client.get(f"/messages/{asked.id}/reply")

    placeholder = client.get(f"/messages/{asked.id}/retry", headers=_HTMX)
    streamed = client.get(f"/messages/{asked.id}/reply")

    assert placeholder.status_code == 200
    assert f'hx-sse:connect="/messages/{asked.id}/reply"' in placeholder.text
    assert "Hey! Good to hear from you." in _sse_data(streamed.text)[-1]
    assert len(conversation.chitchatted) == 2


async def test_another_users_message_gets_404_for_its_reply_and_its_retry() -> None:
    chat = InMemoryChatRepository()
    theirs = await _asked(chat, uuid4())
    conversation = ScriptedConversation()
    client, _, _ = _client(conversation, chat)

    assert client.get(f"/messages/{theirs.id}/reply").status_code == 404
    assert client.get(f"/messages/{theirs.id}/retry", headers=_HTMX).status_code == 404
    assert conversation.routed == []


def test_an_unknown_message_gets_404() -> None:
    client, _, _ = _client()

    assert client.get(f"/messages/{uuid4()}/reply").status_code == 404


def test_a_malformed_message_id_is_refused() -> None:
    client, _, _ = _client()

    assert client.get("/messages/not-a-uuid/reply").status_code == 422


async def test_history_shows_a_reply_as_the_assistants_text_without_a_bubble() -> None:
    client, user_id, chat = _client()
    await _asked(chat, user_id)
    await chat.add(user_id, ChatRole.ASSISTANT, "Hey there.", FROZEN_NOW)

    page = client.get("/").text

    reply = re.search(r'<div class="chat-message flex gap-2\.5"[^>]*>.*?</div>', page, re.S)
    assert reply is not None
    assert "Hey there." in reply.group(0)
    assert "bg-primary/20" not in reply.group(0)


def test_a_sent_message_shows_the_thinking_indicator_until_its_first_token() -> None:
    client, _, chat = _client()

    response = client.post("/messages", data={"message": "hi!"}, headers=_HTMX)

    [(_, asked)] = chat.rows
    indicator = re.search(r"<div[^>]*data-thinking[^>]*>.*?Thinking…", response.text, re.DOTALL)
    assert indicator is not None
    assert f'id="reply-{asked.id}-phase"' in response.text
    # The streaming text row stays hidden until the first chunk lands in its text span.
    assert f'id="reply-{asked.id}-text" data-reply-text aria-hidden="true"></span>' in response.text
    # A static selector: Tailwind can only generate classes it can read from the source.
    assert "group-has-[[data-reply-text]:not(:empty)]/reply:hidden" in response.text
    assert "group-has-[[data-reply-text]:not(:empty)]/reply:flex" in response.text


def test_the_thinking_indicator_stays_still_under_reduced_motion() -> None:
    client, _, _ = _client()

    html = client.post("/messages", data={"message": "hi!"}, headers=_HTMX).text

    animated = re.findall(r'class="[^"]*animate-\[[^"]*"', html)
    assert animated
    assert all("motion-reduce:animate-none" in element for element in animated)


def test_the_thinking_indicator_carries_a_hidden_elapsed_hint() -> None:
    client, _, _ = _client()

    html = client.post("/messages", data={"message": "hi!"}, headers=_HTMX).text

    assert re.search(r"<span[^>]*data-thinking-hint[^>]* hidden[^>]*>", html)
    assert "data-thinking-elapsed" in html


def test_the_chat_page_loads_the_thinking_script() -> None:
    client, _, _ = _client()

    assert "/static/js/thinking.js" in client.get("/").text


async def test_a_phase_event_switches_the_indicators_label() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    routing, replying, *_ = _sse_data(client.get(f"/messages/{asked.id}/reply").text)

    for event, label in ((routing, "Reading your message…"), (replying, "Writing your reply…")):
        assert f'hx-target="#reply-{asked.id}-phase"' in event
        assert 'hx-swap="innerHTML"' in event
        assert label in event


async def test_an_error_before_any_token_replaces_the_indicator_with_the_error_row() -> None:
    conversation = ScriptedConversation(route_outcomes=[LlmCallOutcome.ERROR])
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)

    routing, final = _sse_data(client.get(f"/messages/{asked.id}/reply").text)

    assert "Reading your message…" in routing
    assert f'hx-target="#reply-{asked.id}"' in final
    assert "data-thinking" not in final


_LIVE = re.compile(r'aria-live="(?!off)|role="(?:status|alert|log)"')


class _LiveNesting(HTMLParser):
    """Collects the live regions in a fragment and notes any that sits inside another."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.regions = 0
        self.nested = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "img", "input", "hr", "meta", "link"}:
            return
        rendered = " ".join(f'{name}="{value}"' for name, value in attrs)
        live = bool(_LIVE.search(rendered))
        if live:
            self.regions += 1
            self.nested += any(ancestor for _, ancestor in self.stack)
        self.stack.append((tag, live))

    def handle_endtag(self, tag: str) -> None:
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


def _live_regions(html: str) -> _LiveNesting:
    parser = _LiveNesting()
    parser.feed(html)
    return parser


def test_the_reply_path_has_one_live_region_and_none_nested() -> None:
    client, _, chat = _client()

    sent = client.post("/messages", data={"message": "hi!"}, headers=_HTMX).text

    [(_, asked)] = chat.rows
    assert _live_regions(sent).nested == 0
    wrapper = re.search(rf'<div id="reply-{asked.id}"[^>]*>', sent)
    assert wrapper is not None
    assert 'aria-live="polite"' in wrapper.group(0)
    assert _live_regions(sent).regions == 1


async def test_streamed_text_and_the_elapsed_hint_are_silent_to_screen_readers() -> None:
    client, _, _ = _client()

    html = client.post("/messages", data={"message": "hi!"}, headers=_HTMX).text

    assert re.search(r"<span[^>]*data-reply-text[^>]*aria-hidden=\"true\"", html)
    assert re.search(r"<span[^>]*data-thinking-hint[^>]*aria-hidden=\"true\"", html)


async def test_the_error_row_is_announced_by_the_reply_region_alone() -> None:
    conversation = ScriptedConversation(chitchat_outcomes=[LlmCallOutcome.TIMEOUT])
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)

    final = _sse_data(client.get(f"/messages/{asked.id}/reply").text)[-1]

    assert "took too long" in final
    assert _live_regions(final).regions == 0


async def test_the_saved_reply_swapped_in_is_not_a_live_region_itself() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    final = _sse_data(client.get(f"/messages/{asked.id}/reply").text)[-1]

    assert _live_regions(final).regions == 0


async def test_loading_a_chat_with_history_announces_nothing_on_its_own() -> None:
    client, user_id, chat = _client()
    await _asked(chat, user_id)
    await chat.add(user_id, ChatRole.ASSISTANT, "Hey there.", FROZEN_NOW)

    page = client.get("/").text

    assert 'id="chat-history"' in page
    assert _live_regions(page).regions == 0


async def test_a_page_with_a_pending_reply_has_one_live_region_and_none_nested() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    page = client.get("/").text

    regions = _live_regions(page)
    assert regions.regions == 1
    assert regions.nested == 0
    assert re.search(rf'<div id="reply-{asked.id}"[^>]*aria-live="polite"', page)


def _action(html: str) -> str:
    """The composer's action slot: the one control that is Send or Stop (ticket #116)."""
    match = re.search(r'<span id="chat-action"[^>]*>.*?</span>', html, re.DOTALL)
    assert match is not None
    return match.group(0)


def test_an_idle_composer_offers_send_and_no_stop() -> None:
    client, _, _ = _client()

    action = _action(client.get("/").text)

    assert 'aria-label="Send"' in action
    assert "Stop generating" not in action


def test_a_sent_message_swaps_stop_in_for_send_until_the_reply_ends() -> None:
    client, _, chat = _client()

    response = client.post("/messages", data={"message": "tell me a long story"}, headers=_HTMX)

    [(_, asked)] = chat.rows
    action = _action(response.text)
    assert 'aria-label="Stop generating"' in action
    assert 'aria-label="Send"' not in action
    assert f'hx-get="/messages/{asked.id}/stop"' in action
    assert f'hx-target="#reply-{asked.id}"' in action
    assert 'hx-swap="outerHTML"' in action
    assert 'type="button"' in action


async def test_a_reload_with_a_pending_reply_shows_stop() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    action = _action(client.get("/").text)

    assert f'hx-get="/messages/{asked.id}/stop"' in action
    assert 'aria-label="Send"' not in action


async def test_a_reload_after_the_reply_shows_send() -> None:
    client, user_id, chat = _client()
    await _asked(chat, user_id)
    await chat.add(user_id, ChatRole.ASSISTANT, "Hey there.", FROZEN_NOW)

    action = _action(client.get("/").text)

    assert 'aria-label="Send"' in action


async def test_the_finished_reply_swaps_send_back_in() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    final = _sse_data(client.get(f"/messages/{asked.id}/reply").text)[-1]

    assert 'hx-target="#chat-action"' in final
    assert 'aria-label="Send"' in final
    assert "Stop generating" not in final


async def test_a_failed_reply_swaps_send_back_in() -> None:
    conversation = ScriptedConversation(chitchat_outcomes=[LlmCallOutcome.TIMEOUT])
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)

    final = _sse_data(client.get(f"/messages/{asked.id}/reply").text)[-1]

    assert 'hx-target="#chat-action"' in final
    assert 'aria-label="Send"' in final


async def test_stop_swaps_the_streaming_row_for_stopped_with_retry_and_brings_send_back() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)

    response = client.get(f"/messages/{asked.id}/stop", headers=_HTMX)

    assert response.status_code == 200
    assert f'id="reply-{asked.id}"' in response.text
    assert "hx-sse:connect" not in response.text
    assert "Stopped" in response.text
    assert f'hx-get="/messages/{asked.id}/retry"' in response.text
    assert f'hx-target="#reply-{asked.id}"' in response.text
    assert 'hx-target="#chat-action"' in response.text
    assert 'aria-label="Send"' in response.text


async def test_stopping_runs_no_model_call_and_saves_no_reply() -> None:
    conversation = ScriptedConversation()
    client, user_id, chat = _client(conversation)
    asked = await _asked(chat, user_id)

    client.get(f"/messages/{asked.id}/stop", headers=_HTMX)

    assert conversation.routed == []
    assert [message for _, message in chat.rows] == [asked]


async def test_stopping_a_reply_that_was_already_saved_shows_it_rather_than_stopped() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)
    await chat.add(user_id, ChatRole.ASSISTANT, "Hey there.", FROZEN_NOW)

    response = client.get(f"/messages/{asked.id}/stop", headers=_HTMX)

    assert "Hey there." in response.text
    assert "Stopped" not in response.text
    assert 'aria-label="Send"' in response.text


async def test_retry_after_stop_streams_a_fresh_reply_and_brings_stop_back() -> None:
    client, user_id, chat = _client()
    asked = await _asked(chat, user_id)
    client.get(f"/messages/{asked.id}/stop", headers=_HTMX)

    placeholder = client.get(f"/messages/{asked.id}/retry", headers=_HTMX)
    streamed = client.get(f"/messages/{asked.id}/reply")

    assert f'hx-sse:connect="/messages/{asked.id}/reply"' in placeholder.text
    assert 'hx-target="#chat-action"' in placeholder.text
    assert 'aria-label="Stop generating"' in placeholder.text
    assert "Hey! Good to hear from you." in _sse_data(streamed.text)[-1]


async def test_another_users_message_gets_404_for_its_stop() -> None:
    chat = InMemoryChatRepository()
    theirs = await _asked(chat, uuid4())
    client, _, _ = _client(chat=chat)

    assert client.get(f"/messages/{theirs.id}/stop", headers=_HTMX).status_code == 404


def test_the_stop_icon_is_vendored() -> None:
    from ai_trainer.web.icons import vendored_icon_names

    assert "square" in vendored_icon_names()
