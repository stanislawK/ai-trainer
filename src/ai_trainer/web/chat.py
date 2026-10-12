"""Wires the chat, the main screen: `GET /` (the latest conversation), `GET /c/{id}` (one
conversation), `POST /messages` and the streamed reply, `GET /messages/{id}/reply` and
`GET /messages/{id}/stop` (PRD-0004 F1, B13, B24, ADR-0008, ADR-0012, ADR-0021, tickets #78,
#82, #116 and #146).
`user_id` comes only from the authenticated session (ADR-0005), and a conversation ID from the
URL or the form is always looked up with it: another user's conversation is a 404."""

from collections.abc import AsyncGenerator, AsyncIterable, Sequence
from contextlib import aclosing
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.templating import Jinja2Templates

from ai_trainer.application.chat import (
    MAX_MESSAGE_LENGTH,
    ConversationNotFoundError,
    EmptyMessageError,
    MessageTooLongError,
    latest_conversation,
    list_history,
    open_conversation,
    send_message,
)
from ai_trainer.application.ports.chat import ChatRepositoryPort
from ai_trainer.application.reply import (
    MessageNotFoundError,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyPhase,
    ReplyServices,
    find_message,
    find_saved_reply,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, Conversation
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.users import User
from ai_trainer.web.glyphs import build_reply_glyph

_UNPROCESSABLE = 422
_NOT_FOUND = 404
_MORNING_FROM = 5
_AFTERNOON_FROM = 12
_EVENING_FROM = 18


def _part_of_day(hour: int) -> str:
    if _MORNING_FROM <= hour < _AFTERNOON_FROM:
        return "morning"
    if _AFTERNOON_FROM <= hour < _EVENING_FROM:
        return "afternoon"
    return "evening"


def build_chat_router(
    templates: Jinja2Templates,
    *,
    chat: ChatRepositoryPort,
    replies: ReplyServices,
    sports: SportRegistry,
) -> APIRouter:
    router = APIRouter()
    templates.env.globals["reply_glyph"] = build_reply_glyph(sports)
    clock = replies.clock
    conversations = replies.conversations

    def render(template_name: str, **context: Any) -> str:
        return templates.get_template(template_name).render(**context)

    def reply_event(message_id: UUID, event: ReplyEvent) -> ServerSentEvent:
        """Each event is an htmx partial: a phase relabels the thinking indicator, chunks append
        to the streaming text, and the last event swaps the whole placeholder for the saved
        reply or the error row (ADR-0012)."""
        if isinstance(event, ReplyPhase):
            html = render(
                "partials/chat/reply_phase.html", message_id=message_id, phase=event.phase
            )
        elif isinstance(event, ReplyChunk):
            html = render("partials/chat/reply_chunk.html", message_id=message_id, text=event.text)
        elif isinstance(event, ReplyDone):
            html = render(
                "partials/chat/reply_done.html", message_id=message_id, messages=event.messages
            )
        else:
            html = render(
                "partials/chat/reply_failed.html",
                message_id=message_id,
                timed_out=event.outcome is LlmCallOutcome.TIMEOUT,
            )
        return ServerSentEvent(raw_data=html)

    async def reply_events(request: Request, message_id: UUID) -> AsyncGenerator[ReplyEvent]:
        """Resolved before the response starts, so another user's message is a plain 404."""
        user: User = request.state.user
        try:
            return await start_reply(user.id, message_id, timezone=user.timezone, services=replies)
        except MessageNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None

    def is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def composer(
        request: Request, conversation_id: UUID | None, *, text: str = "", error: str | None = None
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/chat/composer.html",
            {
                "conversation_id": conversation_id,
                "text": text,
                "error": error,
                "max_length": MAX_MESSAGE_LENGTH,
            },
            status_code=_UNPROCESSABLE if error else 200,
        )

    def parse_conversation_id(raw: str) -> UUID:
        """A malformed ID is as unknown as a missing one: a 404, never a 422."""
        try:
            return UUID(raw)
        except ValueError:
            raise HTTPException(status_code=_NOT_FOUND) from None

    async def chat_page(request: Request, conversation: Conversation | None) -> HTMLResponse:
        user: User = request.state.user
        history: Sequence[ChatMessage] = (
            await list_history(user.id, conversation.id, repository=chat)
            if conversation is not None
            else ()
        )
        local_hour = clock.now().astimezone(ZoneInfo(user.timezone)).hour
        first_name = user.name.split()[0] if user.name and user.name.strip() else None
        context = {
            "conversation_id": conversation.id if conversation is not None else None,
            "history": history,
            "part_of_day": _part_of_day(local_hour),
            "first_name": first_name,
            "text": "",
            "error": None,
            "max_length": MAX_MESSAGE_LENGTH,
        }
        template_name = "partials/chat/index.html" if is_htmx(request) else "pages/chat/index.html"
        return templates.TemplateResponse(request, template_name, context)

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """The conversation the athlete was last active in, or an empty chat (ADR-0021)."""
        user: User = request.state.user
        return await chat_page(
            request, await latest_conversation(user.id, conversations=conversations)
        )

    @router.get("/c/{conversation_id}", response_class=HTMLResponse)
    async def show_conversation(request: Request, conversation_id: str) -> HTMLResponse:
        user: User = request.state.user
        try:
            conversation = await open_conversation(
                user.id, parse_conversation_id(conversation_id), conversations=conversations
            )
        except ConversationNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        return await chat_page(request, conversation)

    @router.post("/messages", response_class=HTMLResponse)
    async def post_message(request: Request) -> HTMLResponse:
        """Adds to the conversation the composer shows; with none, the message starts one."""
        user: User = request.state.user
        form = await request.form()
        raw = form.get("message", "")
        text = raw if isinstance(raw, str) else ""
        raw_id = form.get("conversation_id", "")
        conversation_id = (
            parse_conversation_id(raw_id) if isinstance(raw_id, str) and raw_id else None
        )
        try:
            message = await send_message(
                user.id,
                conversation_id,
                text,
                clock=clock,
                chat=chat,
                conversations=conversations,
            )
        except EmptyMessageError:
            return composer(request, conversation_id, error="empty")
        except MessageTooLongError:
            return composer(request, conversation_id, text=text, error="too_long")
        except ConversationNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        return templates.TemplateResponse(
            request,
            "partials/chat/sent.html",
            {
                "message": message,
                "conversation_id": message.conversation_id,
                "text": "",
                "error": None,
                "max_length": MAX_MESSAGE_LENGTH,
            },
        )

    @router.get("/messages/{message_id}/reply", response_class=EventSourceResponse)
    async def stream_reply(
        message_id: UUID,
        events: Annotated[AsyncGenerator[ReplyEvent], Depends(reply_events)],
    ) -> AsyncIterable[ServerSentEvent]:
        # Closing the reply closes the model stream too, so a reader who leaves mid-reply
        # still leaves its `llm_calls` row, as `cancelled` (ADR-0018).
        async with aclosing(events):
            async for event in events:
                yield reply_event(message_id, event)

    @router.get("/messages/{message_id}/retry", response_class=HTMLResponse)
    async def retry_reply(request: Request, message_id: UUID) -> HTMLResponse:
        """A fresh placeholder for the error row's Retry: it connects to the stream again."""
        user: User = request.state.user
        try:
            message = await find_message(user.id, message_id, chat=chat)
        except MessageNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        return templates.TemplateResponse(request, "partials/chat/retry.html", {"message": message})

    @router.get("/messages/{message_id}/stop", response_class=HTMLResponse)
    async def stop_reply(request: Request, message_id: UUID) -> HTMLResponse:
        """The Stop button's answer. Swapping out the streaming row is what closes its stream
        and cancels the call (ADR-0018), so nothing is saved and nothing is called here. A
        reply that finished first is shown as it was saved instead of as stopped."""
        user: User = request.state.user
        try:
            saved = await find_saved_reply(user.id, message_id, chat=chat)
        except MessageNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        if saved:
            return templates.TemplateResponse(
                request,
                "partials/chat/reply_saved.html",
                {"message_id": message_id, "messages": saved},
            )
        return templates.TemplateResponse(
            request, "partials/chat/reply_stopped.html", {"message_id": message_id}
        )

    return router
