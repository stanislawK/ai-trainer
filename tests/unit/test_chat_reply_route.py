"""The streamed reply (PRD-0003 F1, ADR-0008, ADR-0012, ticket #82): `POST /messages` appends a
placeholder that connects to `GET /messages/{id}/reply`, which streams the reply as htmx
partials and ends by swapping in the saved reply, or a retryable error row."""

import re
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
    *chunks, final = _sse_data(response.text)
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
    assert 'role="alert"' in final
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
