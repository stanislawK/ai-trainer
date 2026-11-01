"""The chat page and its composer: `GET /` (the latest conversation), `GET /c/{id}` (one
conversation), `GET /c/new` (an empty one) and `POST /messages` (PRD-0004 F1, F16, ADR-0021,
tickets #78, #146 and #147). Every pending draft and open question of the conversation shows
under the reply that introduced it (#154, #162)."""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ai_trainer.application.chat import (
    MAX_MESSAGE_LENGTH,
    ConversationNotFoundError,
    EmptyMessageError,
    MessageTooLongError,
    latest_conversation,
    list_conversations,
    list_history,
    open_conversation,
    send_message,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole, Conversation
from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import (
    NOT_FOUND,
    UNPROCESSABLE,
    ChatContext,
    is_htmx,
    parse_id,
)
from ai_trainer.web.clarifications import ChoiceCard, choice_cards
from ai_trainer.web.conversations import conversation_rows
from ai_trainer.web.drafts import DraftCard, draft_cards

_MORNING_FROM = 5
_AFTERNOON_FROM = 12
_EVENING_FROM = 18


def _part_of_day(hour: int) -> str:
    if _MORNING_FROM <= hour < _AFTERNOON_FROM:
        return "morning"
    if _AFTERNOON_FROM <= hour < _EVENING_FROM:
        return "afternoon"
    return "evening"


class _ThreadRoutes:
    def __init__(self, ctx: ChatContext) -> None:
        self._ctx = ctx

    async def _window_log(self, user: User, history: Sequence[ChatMessage]) -> UUID | None:
        """The athlete's message a history window opens on the reply to, when the window cut
        that message off: its draft cards still belong under that reply (ticket #154)."""
        if not history or history[0].role is not ChatRole.ASSISTANT:
            return None
        before = await self._ctx.chat.list_before(user.id, history[0], 1)
        return before[0].id if before and before[0].role is ChatRole.USER else None

    async def _conversation_list(self, user: User, current: UUID | None) -> dict[str, Any]:
        """The conversation list's context: the athlete's rows, which one is open, and the
        retention window it covers (B25, B26, F17, ticket #213)."""
        ctx = self._ctx
        found = await list_conversations(user.id, conversations=ctx.conversations)
        return {
            "conversations": conversation_rows(found, now=ctx.clock.now(), timezone=user.timezone),
            "current_conversation_id": current,
            "retention_days": ctx.retention_days,
        }

    def _composer(
        self,
        request: Request,
        conversation_id: UUID | None,
        *,
        text: str = "",
        error: str | None = None,
    ) -> HTMLResponse:
        return self._ctx.templates.TemplateResponse(
            request,
            "partials/chat/composer.html",
            {
                "conversation_id": conversation_id,
                "text": text,
                "error": error,
                "max_length": MAX_MESSAGE_LENGTH,
            },
            status_code=UNPROCESSABLE if error else 200,
        )

    async def _chat_page(self, request: Request, conversation: Conversation | None) -> HTMLResponse:
        ctx = self._ctx
        user: User = request.state.user
        history: Sequence[ChatMessage] = ()
        cards: Mapping[UUID, Sequence[DraftCard]] = {}
        questions: Mapping[UUID, ChoiceCard] = {}
        if conversation is not None:
            history = await list_history(user.id, conversation.id, repository=ctx.chat)
            shown = await ctx.replies.drafts.list_shown(user.id, conversation.id)
            asked = await ctx.replies.clarifications.list_shown(user.id, conversation.id)
            log_id = await self._window_log(user, history)
            cards = draft_cards(
                history,
                shown,
                sports=ctx.sports,
                today=ctx.local_today(user),
                athlete_sports=await ctx.athlete_sports(user),
                log_id=log_id,
                questions=asked,
                labels=await ctx.draft_labels(user, shown, asked),
                answers=ctx.draft_answers(shown, asked),
            )
            questions = choice_cards(history, asked, log_id=log_id)
        local_hour = ctx.clock.now().astimezone(ZoneInfo(user.timezone)).hour
        first_name = user.name.split()[0] if user.name and user.name.strip() else None
        context = {
            "conversation_id": conversation.id if conversation is not None else None,
            "history": history,
            "draft_cards": cards,
            "choice_cards": questions,
            "part_of_day": _part_of_day(local_hour),
            "first_name": first_name,
            "text": "",
            "error": None,
            "max_length": MAX_MESSAGE_LENGTH,
            # An htmx swap replaces only the chat, so the lists follow out of band.
            "list_oob": is_htmx(request),
            **await self._conversation_list(
                user, conversation.id if conversation is not None else None
            ),
        }
        template_name = "partials/chat/index.html" if is_htmx(request) else "pages/chat/index.html"
        return ctx.templates.TemplateResponse(request, template_name, context)

    async def index(self, request: Request) -> HTMLResponse:
        """The conversation the athlete was last active in today, or an empty chat (ADR-0021)."""
        ctx = self._ctx
        user: User = request.state.user
        return await self._chat_page(
            request,
            await latest_conversation(
                user.id, conversations=ctx.conversations, clock=ctx.clock, timezone=user.timezone
            ),
        )

    async def new_conversation(self, request: Request) -> HTMLResponse:
        """An empty chat with starter chips; nothing is stored until its first message (F16,
        ADR-0021). Declared before `/c/{conversation_id}`, which would read `new` as an ID."""
        return await self._chat_page(request, None)

    async def show_conversation(self, request: Request, conversation_id: str) -> HTMLResponse:
        user: User = request.state.user
        try:
            conversation = await open_conversation(
                user.id, parse_id(conversation_id), conversations=self._ctx.conversations
            )
        except ConversationNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        return await self._chat_page(request, conversation)

    async def post_message(self, request: Request) -> HTMLResponse:
        """Adds to the conversation the composer shows; with none, the message starts one."""
        ctx = self._ctx
        user: User = request.state.user
        form = await request.form()
        raw = form.get("message", "")
        text = raw if isinstance(raw, str) else ""
        raw_id = form.get("conversation_id", "")
        conversation_id = parse_id(raw_id) if isinstance(raw_id, str) and raw_id else None
        try:
            message = await send_message(
                user.id,
                conversation_id,
                text,
                clock=ctx.clock,
                chat=ctx.chat,
                conversations=ctx.conversations,
            )
        except EmptyMessageError:
            return self._composer(request, conversation_id, error="empty")
        except MessageTooLongError:
            return self._composer(request, conversation_id, text=text, error="too_long")
        except ConversationNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        response = ctx.templates.TemplateResponse(
            request,
            "partials/chat/sent.html",
            {
                "message": message,
                "conversation_id": message.conversation_id,
                "text": "",
                "error": None,
                "max_length": MAX_MESSAGE_LENGTH,
                **await self._conversation_list(user, message.conversation_id),
            },
        )
        if conversation_id is None:
            # The first message started a conversation: the address follows it, so a reload
            # opens it rather than an empty chat.
            response.headers["HX-Replace-Url"] = f"/c/{message.conversation_id}"
        return response


def build_thread_router(ctx: ChatContext) -> APIRouter:
    router = APIRouter()
    routes = _ThreadRoutes(ctx)
    router.get("/", response_class=HTMLResponse)(routes.index)
    router.get("/c/new", response_class=HTMLResponse)(routes.new_conversation)
    router.get("/c/{conversation_id}", response_class=HTMLResponse)(routes.show_conversation)
    router.post("/messages", response_class=HTMLResponse)(routes.post_message)
    return router
