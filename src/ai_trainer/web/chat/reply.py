"""The streamed reply and its controls: `GET /messages/{id}/reply`, its Retry and Stop, and a
failed draft's Retry (PRD-0004 B13, B24, ADR-0008, ADR-0012, ADR-0018, tickets #82, #116 and
#161). A pending session draft shows as a card under the reply part that introduced it, on the
stream's end (PRD-0004 F2, ticket #154)."""

from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from ai_trainer.application.reply import (
    FailedPartNotFoundError,
    MessageNotFoundError,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyPhase,
    RetryFailed,
    find_message,
    find_saved_reply,
    retry_draft,
    start_reply,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import NOT_FOUND, ChatContext, parse_id
from ai_trainer.web.clarifications import choice_cards


class _ReplyRoutes:
    def __init__(self, ctx: ChatContext) -> None:
        self._ctx = ctx

    async def reply_event(self, user: User, message_id: UUID, event: ReplyEvent) -> ServerSentEvent:
        """Each event is an htmx partial: a phase relabels the thinking indicator, chunks append
        to the streaming text, and the last event swaps the whole placeholder for the saved
        reply or the error row (ADR-0012)."""
        ctx = self._ctx
        if isinstance(event, ReplyPhase):
            html = ctx.render(
                "partials/chat/reply_phase.html", message_id=message_id, phase=event.phase
            )
        elif isinstance(event, ReplyChunk):
            html = ctx.render(
                "partials/chat/reply_chunk.html", message_id=message_id, text=event.text
            )
        elif isinstance(event, ReplyDone):
            html = ctx.render(
                "partials/chat/reply_done.html",
                message_id=message_id,
                messages=event.messages,
                draft_cards=await ctx.reply_cards(user, message_id, event),
                choice_cards=choice_cards(event.messages, event.clarifications, log_id=message_id),
            )
        else:
            html = ctx.render(
                "partials/chat/reply_failed.html",
                message_id=message_id,
                timed_out=event.outcome is LlmCallOutcome.TIMEOUT,
                limited=event.outcome is LlmCallOutcome.RATE_LIMITED,
            )
        return ServerSentEvent(raw_data=html)

    async def reply_events(self, request: Request, message_id: UUID) -> AsyncGenerator[ReplyEvent]:
        """Resolved before the response starts, so another user's message is a plain 404."""
        user: User = request.state.user
        try:
            return await start_reply(
                user.id, message_id, timezone=user.timezone, services=self._ctx.replies
            )
        except MessageNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None

    async def retry_reply(self, request: Request, message_id: UUID) -> HTMLResponse:
        """A fresh placeholder for the error row's Retry: it connects to the stream again."""
        ctx = self._ctx
        user: User = request.state.user
        try:
            message = await find_message(
                user.id, message_id, chat=ctx.chat, conversations=ctx.conversations
            )
        except MessageNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        return ctx.templates.TemplateResponse(
            request, "partials/chat/retry.html", {"message": message}
        )

    async def stop_reply(self, request: Request, message_id: UUID) -> HTMLResponse:
        """The Stop button's answer. Swapping out the streaming row is what closes its stream
        and cancels the call (ADR-0018), so nothing is saved and nothing is called here. A
        reply that finished first is shown as it was saved instead of as stopped."""
        ctx = self._ctx
        user: User = request.state.user
        try:
            saved = await find_saved_reply(user.id, message_id, services=ctx.replies)
        except MessageNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        if saved.messages:
            return ctx.templates.TemplateResponse(
                request,
                "partials/chat/reply_saved.html",
                {
                    "message_id": message_id,
                    "messages": saved.messages,
                    "draft_cards": await ctx.reply_cards(user, message_id, saved),
                    "choice_cards": choice_cards(
                        saved.messages, saved.clarifications, log_id=message_id
                    ),
                },
            )
        return ctx.templates.TemplateResponse(
            request, "partials/chat/reply_stopped.html", {"message_id": message_id}
        )

    async def retry_failed_draft(self, request: Request, part_id: str) -> HTMLResponse:
        """A failed draft's Retry (ticket #161): re-runs its sport's extraction on its log and
        answers with the part as the draft's intro line and its card, which replace the row; no
        session is written. Anything but the user's failed draft, or one already retried, is a
        404 with no model call. A failed extraction answers with the row again and a note."""
        ctx = self._ctx
        user: User = request.state.user
        parsed_id = parse_id(part_id)
        try:
            retried = await retry_draft(
                user.id, parsed_id, timezone=user.timezone, services=ctx.replies
            )
        except FailedPartNotFoundError, MessageNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        if isinstance(retried, RetryFailed):
            return ctx.templates.TemplateResponse(
                request,
                "partials/chat/message.html",
                {
                    "message": retried.part,
                    "draft_cards": {},
                    "choice_cards": {},
                    "retry_error": True,
                },
            )
        return ctx.templates.TemplateResponse(
            request,
            "partials/chat/message.html",
            {
                "message": retried.part,
                "draft_cards": {retried.part.id: [await ctx.shown_card(user, retried.draft)]},
                "choice_cards": {},
            },
        )


def build_reply_router(ctx: ChatContext) -> APIRouter:
    router = APIRouter()
    routes = _ReplyRoutes(ctx)

    @router.get("/messages/{message_id}/reply", response_class=EventSourceResponse)
    async def stream_reply(
        request: Request,
        message_id: UUID,
        events: Annotated[AsyncGenerator[ReplyEvent], Depends(routes.reply_events)],
    ) -> AsyncIterable[ServerSentEvent]:
        user: User = request.state.user
        # Closing the reply closes the model stream too, so a reader who leaves mid-reply
        # still leaves its `llm_calls` row, as `cancelled` (ADR-0018).
        async with aclosing(events):
            async for event in events:
                yield await routes.reply_event(user, message_id, event)

    router.get("/messages/{message_id}/retry", response_class=HTMLResponse)(routes.retry_reply)
    router.get("/messages/{message_id}/stop", response_class=HTMLResponse)(routes.stop_reply)
    router.post("/messages/{part_id}/retry-draft", response_class=HTMLResponse)(
        routes.retry_failed_draft
    )
    return router
