"""Wires the chat, the main screen: `GET /` (the latest conversation), `GET /c/{id}` (one
conversation), `GET /c/new` (an empty one), `POST /messages` and the streamed reply,
`GET /messages/{id}/reply` and `GET /messages/{id}/stop` (PRD-0004 F1, B13, B24, F16, ADR-0008,
ADR-0012, ADR-0021, tickets #78, #82, #116, #146 and #147). A pending session draft shows as a
card under the reply part that introduced it, on the stream's end and on every page load
(PRD-0004 F2, ticket #154). A question that blocks a log is a choice card in place of its line,
answered or skipped with one POST (B17, F9, ADR-0015, ticket #162). A pending card's sport chip
moves the draft to another of the athlete's sports with one POST (B23, ticket #163). A draft
that came later on the day of a same-sport session asks on its card whether it is that session,
answered with the same POST as any choice card (B16, ticket #166). A sport
whose extraction failed in a mixed log shows a row whose Retry drafts it with one POST (#161).
`user_id` comes only from the authenticated session (ADR-0005), and a conversation ID from the
URL or the form is always looked up with it: another user's conversation is a 404."""

from collections.abc import AsyncGenerator, AsyncIterable, Mapping, Sequence
from contextlib import aclosing
from datetime import date
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
from ai_trainer.application.drafts import (
    confirm_draft,
    discard_draft,
    draft_to_edit,
    edit_draft,
    find_draft,
    undo_log,
)
from ai_trainer.application.ports.chat import ChatRepositoryPort
from ai_trainer.application.reply import (
    ClarificationAnswered,
    FailedPartNotFoundError,
    MessageNotFoundError,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    ReplyPhase,
    ReplyServices,
    RetryFailed,
    SessionSettled,
    abandon_clarification,
    answer_clarification,
    asking_sports,
    change_draft_sport,
    find_message,
    find_saved_reply,
    retry_draft,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole, Conversation
from ai_trainer.domain.clarification import (
    ClarificationNotFoundError,
    ClarificationResolvedError,
    UnknownOptionError,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import (
    DraftAwaitingAnswerError,
    DraftConfirmedError,
    DraftDiscardedError,
    DraftEditError,
    DraftNotFoundError,
    DraftSportNotOfferedError,
    SessionNotFoundError,
    StoredDraft,
)
from ai_trainer.domain.users import User
from ai_trainer.web.clarifications import ChoiceCard, choice_cards
from ai_trainer.web.drafts import (
    DraftCard,
    build_failed_draft_sport,
    draft_card,
    draft_cards,
    form_changes,
    open_questions,
)
from ai_trainer.web.glyphs import build_reply_glyph

_UNPROCESSABLE = 422
_NOT_FOUND = 404
_CONFLICT = 409
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
    templates.env.globals["failed_draft_sport"] = build_failed_draft_sport(sports)
    clock = replies.clock
    conversations = replies.conversations

    def render(template_name: str, **context: Any) -> str:
        return templates.get_template(template_name).render(**context)

    def local_today(user: User) -> date:
        return clock.now().astimezone(ZoneInfo(user.timezone)).date()

    async def athlete_sports(user: User) -> Sequence[str]:
        """The athlete's own sports, the ones a pending card's chip can offer (#163)."""
        return await replies.profile.list_sports(user.id)

    async def reply_cards(
        user: User, message_id: UUID, done: ReplyDone
    ) -> Mapping[UUID, Sequence[DraftCard]]:
        return draft_cards(
            done.messages,
            done.drafts,
            sports=sports,
            today=local_today(user),
            athlete_sports=await athlete_sports(user) if done.drafts else (),
            log_id=message_id,
            questions=done.clarifications,
        )

    async def window_log(user: User, history: Sequence[ChatMessage]) -> UUID | None:
        """The athlete's message a history window opens on the reply to, when the window cut
        that message off: its draft cards still belong under that reply (ticket #154)."""
        if not history or history[0].role is not ChatRole.ASSISTANT:
            return None
        before = await chat.list_before(user.id, history[0], 1)
        return before[0].id if before and before[0].role is ChatRole.USER else None

    async def reply_event(user: User, message_id: UUID, event: ReplyEvent) -> ServerSentEvent:
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
                "partials/chat/reply_done.html",
                message_id=message_id,
                messages=event.messages,
                draft_cards=await reply_cards(user, message_id, event),
                choice_cards=choice_cards(event.messages, event.clarifications, log_id=message_id),
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

    def parse_id(raw: str) -> UUID:
        """A malformed ID is as unknown as a missing one: a 404, never a 422."""
        try:
            return UUID(raw)
        except ValueError:
            raise HTTPException(status_code=_NOT_FOUND) from None

    async def chat_page(request: Request, conversation: Conversation | None) -> HTMLResponse:
        user: User = request.state.user
        history: Sequence[ChatMessage] = ()
        cards: Mapping[UUID, Sequence[DraftCard]] = {}
        questions: Mapping[UUID, ChoiceCard] = {}
        if conversation is not None:
            history = await list_history(user.id, conversation.id, repository=chat)
            shown = await replies.drafts.list_shown(user.id, conversation.id)
            asked = await replies.clarifications.list_shown(user.id, conversation.id)
            log_id = await window_log(user, history)
            cards = draft_cards(
                history,
                shown,
                sports=sports,
                today=local_today(user),
                athlete_sports=await athlete_sports(user),
                log_id=log_id,
                questions=asked,
            )
            questions = choice_cards(history, asked, log_id=log_id)
        local_hour = clock.now().astimezone(ZoneInfo(user.timezone)).hour
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

    @router.get("/c/new", response_class=HTMLResponse)
    async def new_conversation(request: Request) -> HTMLResponse:
        """An empty chat with starter chips; nothing is stored until its first message (F16,
        ADR-0021). Declared before `/c/{conversation_id}`, which would read `new` as an ID."""
        return await chat_page(request, None)

    @router.get("/c/{conversation_id}", response_class=HTMLResponse)
    async def show_conversation(request: Request, conversation_id: str) -> HTMLResponse:
        user: User = request.state.user
        try:
            conversation = await open_conversation(
                user.id, parse_id(conversation_id), conversations=conversations
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
        conversation_id = parse_id(raw_id) if isinstance(raw_id, str) and raw_id else None
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
        response = templates.TemplateResponse(
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
        if conversation_id is None:
            # The first message started a conversation: the address follows it, so a reload
            # opens it rather than an empty chat.
            response.headers["HX-Replace-Url"] = f"/c/{message.conversation_id}"
        return response

    @router.get("/messages/{message_id}/reply", response_class=EventSourceResponse)
    async def stream_reply(
        request: Request,
        message_id: UUID,
        events: Annotated[AsyncGenerator[ReplyEvent], Depends(reply_events)],
    ) -> AsyncIterable[ServerSentEvent]:
        user: User = request.state.user
        # Closing the reply closes the model stream too, so a reader who leaves mid-reply
        # still leaves its `llm_calls` row, as `cancelled` (ADR-0018).
        async with aclosing(events):
            async for event in events:
                yield await reply_event(user, message_id, event)

    @router.get("/messages/{message_id}/retry", response_class=HTMLResponse)
    async def retry_reply(request: Request, message_id: UUID) -> HTMLResponse:
        """A fresh placeholder for the error row's Retry: it connects to the stream again."""
        user: User = request.state.user
        try:
            message = await find_message(
                user.id, message_id, chat=chat, conversations=conversations
            )
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
            saved = await find_saved_reply(user.id, message_id, services=replies)
        except MessageNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        if saved.messages:
            return templates.TemplateResponse(
                request,
                "partials/chat/reply_saved.html",
                {
                    "message_id": message_id,
                    "messages": saved.messages,
                    "draft_cards": await reply_cards(user, message_id, saved),
                    "choice_cards": choice_cards(
                        saved.messages, saved.clarifications, log_id=message_id
                    ),
                },
            )
        return templates.TemplateResponse(
            request, "partials/chat/reply_stopped.html", {"message_id": message_id}
        )

    @router.post("/messages/{part_id}/retry-draft", response_class=HTMLResponse)
    async def retry_failed_draft(request: Request, part_id: str) -> HTMLResponse:
        """A failed draft's Retry (ticket #161): re-runs its sport's extraction on its log and
        answers with the part as the draft's intro line and its card, which replace the row; no
        session is written. Anything but the user's failed draft, or one already retried, is a
        404 with no model call. A failed extraction answers with the row again and a note."""
        user: User = request.state.user
        parsed_id = parse_id(part_id)
        try:
            retried = await retry_draft(
                user.id, parsed_id, timezone=user.timezone, services=replies
            )
        except FailedPartNotFoundError, MessageNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        if isinstance(retried, RetryFailed):
            return templates.TemplateResponse(
                request,
                "partials/chat/message.html",
                {
                    "message": retried.part,
                    "draft_cards": {},
                    "choice_cards": {},
                    "retry_error": True,
                },
            )
        return templates.TemplateResponse(
            request,
            "partials/chat/message.html",
            {
                "message": retried.part,
                "draft_cards": {retried.part.id: [await shown_card(user, retried.draft)]},
                "choice_cards": {},
            },
        )

    async def shown_card(user: User, draft: StoredDraft) -> DraftCard:
        """A pending draft whose payload no longer reads has no card, so it is not found. A
        pending one asks its open same-session question (#166), and its chip offers no sport
        still asking how to read its log (#164)."""
        pending = draft.status == "pending"
        asked = (
            await replies.clarifications.list_shown(user.id, draft.conversation_id)
            if pending
            else ()
        )
        card = draft_card(
            draft,
            sports=sports,
            today=local_today(user),
            athlete_sports=await athlete_sports(user) if pending else (),
            question=open_questions(asked).get(draft.id),
            asking=asking_sports(asked, draft.message_id),
        )
        if card is None:
            raise HTTPException(status_code=_NOT_FOUND)
        return card

    def edit_form(
        request: Request,
        card: DraftCard,
        *,
        values: Mapping[str, str] | None = None,
        errors: frozenset[str] = frozenset(),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/chat/draft_edit.html",
            {"card": card, "values": values or {}, "errors": errors},
            status_code=_UNPROCESSABLE if errors else 200,
        )

    @router.get("/drafts/{draft_id}", response_class=HTMLResponse)
    async def show_draft(request: Request, draft_id: str) -> HTMLResponse:
        """The draft as it is stored: the pending card, or the row it collapsed to. The edit
        form's Cancel swaps it back in (ticket #158)."""
        user: User = request.state.user
        try:
            draft = await find_draft(user.id, parse_id(draft_id), drafts=replies.drafts)
        except DraftNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        card = await shown_card(user, draft)
        return templates.TemplateResponse(request, card.template, {"card": card, "draft": draft})

    @router.get("/drafts/{draft_id}/edit", response_class=HTMLResponse)
    async def open_edit(request: Request, draft_id: str) -> HTMLResponse:
        """The card's Edit, or its date chip (F2, ADR-0014 invariant 4): the pending draft's
        fields as an inline form, which replaces the card. A confirmed or discarded draft is a
        409."""
        user: User = request.state.user
        try:
            draft = await draft_to_edit(user.id, parse_id(draft_id), drafts=replies.drafts)
        except DraftNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        except DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=_CONFLICT) from None
        return edit_form(request, await shown_card(user, draft))

    @router.post("/drafts/{draft_id}/edit", response_class=HTMLResponse)
    async def save_edit(request: Request, draft_id: str) -> HTMLResponse:
        """The edit form's Save (F2): updates the pending draft and answers with its card,
        which the athlete then confirms as usual. A rejected value is a 422 with the form as
        typed and that field marked, and the draft unchanged. A confirmed or discarded draft
        is a 409."""
        user: User = request.state.user
        parsed_id = parse_id(draft_id)
        form = await request.form()
        values = {key: value for key, value in form.multi_items() if isinstance(value, str)}
        try:
            draft = await edit_draft(
                user.id,
                parsed_id,
                form_changes(values.items()),
                drafts=replies.drafts,
                sports=sports,
            )
        except DraftNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        except DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=_CONFLICT) from None
        except DraftEditError as error:
            stored = await find_draft(user.id, parsed_id, drafts=replies.drafts)
            return edit_form(
                request, await shown_card(user, stored), values=values, errors=error.fields
            )
        card = await shown_card(user, draft)
        return templates.TemplateResponse(request, card.template, {"card": card, "draft": draft})

    @router.post("/drafts/{draft_id}/confirm", response_class=HTMLResponse)
    async def confirm(request: Request, draft_id: str) -> HTMLResponse:
        """The draft card's Confirm (F2, ADR-0022): saves the athlete's draft once and answers
        with its "Logged … to <date>" row, which replaces the card. A second confirm answers
        with the same row. A discarded draft, or one whose card still asks whether it is the
        same session (#166), is a 409 and writes nothing."""
        user: User = request.state.user
        try:
            draft = await confirm_draft(
                user.id, parse_id(draft_id), drafts=replies.drafts, clock=clock
            )
        except DraftNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        except DraftDiscardedError, DraftAwaitingAnswerError:
            raise HTTPException(status_code=_CONFLICT) from None
        card = draft_card(draft, sports=sports, today=local_today(user))
        return templates.TemplateResponse(
            request, "partials/chat/draft_confirmed.html", {"card": card, "draft": draft}
        )

    @router.post("/drafts/{draft_id}/discard", response_class=HTMLResponse)
    async def discard(request: Request, draft_id: str) -> HTMLResponse:
        """The draft card's Discard (F2, ADR-0022): throws the athlete's draft away without
        saving a session and answers with its "Draft discarded" row, which replaces the card.
        A second discard answers with the same row. A confirmed draft is a 409 and changes
        nothing."""
        user: User = request.state.user
        try:
            draft = await discard_draft(
                user.id, parse_id(draft_id), drafts=replies.drafts, clock=clock
            )
        except DraftNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        except DraftConfirmedError:
            raise HTTPException(status_code=_CONFLICT) from None
        card = draft_card(draft, sports=sports, today=local_today(user))
        return templates.TemplateResponse(
            request, "partials/chat/draft_discarded.html", {"card": card, "draft": draft}
        )

    @router.post("/drafts/{draft_id}/undo", response_class=HTMLResponse)
    async def undo(request: Request, draft_id: str) -> HTMLResponse:
        """The confirmed row's Undo (B4, ADR-0022): takes the athlete's just-saved log back from
        its session, once the browser's prompt is accepted, and answers with the "Removed" row,
        which replaces the confirmed one. The session's other logs stay (#165). Another user's
        or an unknown draft, and one with no session left to undo, is a 404 and nothing is
        deleted."""
        user: User = request.state.user
        try:
            draft = await undo_log(
                user.id, parse_id(draft_id), drafts=replies.drafts, clock=replies.clock
            )
        except DraftNotFoundError, SessionNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        card = draft_card(draft, sports=sports, today=local_today(user))
        return templates.TemplateResponse(
            request, "partials/chat/draft_removed.html", {"card": card, "draft": draft}
        )

    @router.post("/drafts/{draft_id}/sport", response_class=HTMLResponse)
    async def change_sport(request: Request, draft_id: str) -> HTMLResponse:
        """The draft card's sport chip (B23, F2, ticket #163): re-runs the picked sport's
        extraction on the draft's own log and answers with the new card, which replaces the
        old one; no session is written. A sport the chip never offered is a 422, a confirmed or
        discarded draft a 409, and another user's or an unknown one a 404, none of them calling
        the model. A failed extraction answers with the card as it was and a note to pick
        again."""
        user: User = request.state.user
        parsed_id = parse_id(draft_id)
        form = await request.form()
        sport_id = form.get("sport_id")
        try:
            changed = await change_draft_sport(
                user.id,
                parsed_id,
                sport_id if isinstance(sport_id, str) else "",
                timezone=user.timezone,
                services=replies,
            )
        except DraftNotFoundError, MessageNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        except DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=_CONFLICT) from None
        except DraftSportNotOfferedError:
            raise HTTPException(status_code=_UNPROCESSABLE) from None
        if isinstance(changed, ReplyFailed):
            card = await shown_card(
                user, await find_draft(user.id, parsed_id, drafts=replies.drafts)
            )
            return templates.TemplateResponse(
                request, card.template, {"card": card, "sport_error": True}
            )
        card = await shown_card(user, changed)
        return templates.TemplateResponse(request, card.template, {"card": card})

    async def choice_answer(
        request: Request, answered: ClarificationAnswered, *, user: User
    ) -> HTMLResponse:
        reply = answered.reply
        athlete = await athlete_sports(user)
        return templates.TemplateResponse(
            request,
            "partials/chat/choice_answer.html",
            {
                "card": ChoiceCard(answered.clarification),
                "messages": reply.messages,
                # A sport answered may ask how to read the log next (ticket #164).
                "choice_cards": choice_cards(
                    reply.messages,
                    reply.clarifications,
                    log_id=answered.clarification.message_id,
                ),
                "draft_cards": draft_cards(
                    reply.messages,
                    reply.drafts,
                    sports=sports,
                    today=local_today(user),
                    athlete_sports=athlete,
                    log_id=answered.clarification.message_id,
                    # Every question of the conversation: a sport still asking how to read
                    # this log is no chip on its cards (ticket #164).
                    questions=await replies.clarifications.list_shown(
                        user.id, answered.clarification.conversation_id
                    ),
                ),
            },
        )

    @router.post("/clarifications/{clarification_id}/answer", response_class=HTMLResponse)
    async def answer(request: Request, clarification_id: str) -> HTMLResponse:
        """A choice card's tap (B17, B23, F9, ADR-0015): the option IDs the athlete picked
        resume the log's extraction in that sport, and the card collapses to the chosen option
        above the new draft. An option the question never offered is a 422, an answered or
        skipped question a 409, another user's or an unknown one a 404, and a failed extraction
        the card again with a retry note, still open; none of them writes anything. A draft's
        same-session question answers with the settled draft card, which replaces the one that
        asked; on a confirmed or discarded draft it is a 409 (#166)."""
        user: User = request.state.user
        parsed_id = parse_id(clarification_id)
        form = await request.form()
        option_ids = [value for value in form.getlist("option_ids") if isinstance(value, str)]
        try:
            answered = await answer_clarification(
                user.id, parsed_id, option_ids, timezone=user.timezone, services=replies
            )
        except ClarificationNotFoundError, MessageNotFoundError, DraftNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        except ClarificationResolvedError, DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=_CONFLICT) from None
        except UnknownOptionError:
            raise HTTPException(status_code=_UNPROCESSABLE) from None
        if isinstance(answered, SessionSettled):
            settled = await shown_card(user, answered.draft)
            return templates.TemplateResponse(request, settled.template, {"card": settled})
        if isinstance(answered, ReplyFailed):
            card = ChoiceCard(await replies.clarifications.get(user.id, parsed_id))
            return templates.TemplateResponse(
                request,
                "partials/chat/choice_card.html",
                {"card": card, "error": True},
            )
        return await choice_answer(request, answered, user=user)

    @router.post("/clarifications/{clarification_id}/abandon", response_class=HTMLResponse)
    async def abandon(request: Request, clarification_id: str) -> HTMLResponse:
        """A choice card's "Let me type instead" (F9, invariant 2 of ADR-0015): closes the
        question and writes nothing else; the composer takes focus after the swap. An answered
        or skipped question is a 409, another user's or an unknown one a 404."""
        user: User = request.state.user
        try:
            skipped = await abandon_clarification(
                user.id, parse_id(clarification_id), services=replies
            )
        except ClarificationNotFoundError:
            raise HTTPException(status_code=_NOT_FOUND) from None
        except ClarificationResolvedError:
            raise HTTPException(status_code=_CONFLICT) from None
        return templates.TemplateResponse(
            request, "partials/chat/choice_card.html", {"card": ChoiceCard(skipped)}
        )

    return router
