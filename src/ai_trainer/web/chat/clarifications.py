"""The choice card's answers: a tap on its options, or "Let me type instead" (B16, B17, B23,
F9, ADR-0015, tickets #162, #164 and #166)."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ai_trainer.application.reply import (
    ClarificationAnswered,
    MessageNotFoundError,
    ReplyFailed,
    SessionSettled,
    abandon_clarification,
    answer_clarification,
)
from ai_trainer.domain.clarification import (
    ClarificationNotFoundError,
    ClarificationResolvedError,
    UnknownOptionError,
)
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
)
from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import (
    CONFLICT,
    NOT_FOUND,
    UNPROCESSABLE,
    ChatContext,
    parse_id,
)
from ai_trainer.web.clarifications import ChoiceCard, choice_cards
from ai_trainer.web.drafts import draft_cards


class _ClarificationRoutes:
    def __init__(self, ctx: ChatContext) -> None:
        self._ctx = ctx

    async def _choice_answer(
        self, request: Request, answered: ClarificationAnswered, *, user: User
    ) -> HTMLResponse:
        ctx = self._ctx
        reply = answered.reply
        athlete = await ctx.athlete_sports(user)
        return ctx.templates.TemplateResponse(
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
                    sports=ctx.sports,
                    today=ctx.local_today(user),
                    athlete_sports=athlete,
                    log_id=answered.clarification.message_id,
                    # Every question of the conversation: a sport still asking how to read
                    # this log is no chip on its cards (ticket #164).
                    questions=await ctx.replies.clarifications.list_shown(
                        user.id, answered.clarification.conversation_id
                    ),
                ),
            },
        )

    async def answer(self, request: Request, clarification_id: str) -> HTMLResponse:
        """A choice card's tap (B17, B23, F9, ADR-0015): the option IDs the athlete picked
        resume the log's extraction in that sport, and the card collapses to the chosen option
        above the new draft. An option the question never offered is a 422, an answered or
        skipped question a 409, another user's or an unknown one a 404, and a failed extraction
        the card again with a retry note, still open; none of them writes anything. A draft's
        same-session question answers with the settled draft card, which replaces the one that
        asked; on a confirmed or discarded draft it is a 409 (#166)."""
        ctx = self._ctx
        user: User = request.state.user
        parsed_id = parse_id(clarification_id)
        form = await request.form()
        option_ids = [value for value in form.getlist("option_ids") if isinstance(value, str)]
        try:
            answered = await answer_clarification(
                user.id, parsed_id, option_ids, timezone=user.timezone, services=ctx.replies
            )
        except ClarificationNotFoundError, MessageNotFoundError, DraftNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except ClarificationResolvedError, DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=CONFLICT) from None
        except UnknownOptionError:
            raise HTTPException(status_code=UNPROCESSABLE) from None
        if isinstance(answered, SessionSettled):
            settled = await ctx.shown_card(user, answered.draft)
            return ctx.templates.TemplateResponse(request, settled.template, {"card": settled})
        if isinstance(answered, ReplyFailed):
            card = ChoiceCard(await ctx.replies.clarifications.get(user.id, parsed_id))
            return ctx.templates.TemplateResponse(
                request,
                "partials/chat/choice_card.html",
                {"card": card, "error": True},
            )
        return await self._choice_answer(request, answered, user=user)

    async def abandon(self, request: Request, clarification_id: str) -> HTMLResponse:
        """A choice card's "Let me type instead" (F9, invariant 2 of ADR-0015): closes the
        question and writes nothing else; the composer takes focus after the swap. On a draft's
        exercise question it answers with the draft's card instead, which replaces the one that
        asked (#397). An answered or skipped question, or one on a confirmed or discarded draft,
        is a 409, another user's or an unknown one a 404."""
        user: User = request.state.user
        try:
            skipped = await abandon_clarification(
                user.id, parse_id(clarification_id), services=self._ctx.replies
            )
        except ClarificationNotFoundError, DraftNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except ClarificationResolvedError, DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=CONFLICT) from None
        if isinstance(skipped, SessionSettled):
            settled = await self._ctx.shown_card(user, skipped.draft)
            return self._ctx.templates.TemplateResponse(
                request, settled.template, {"card": settled}
            )
        return self._ctx.templates.TemplateResponse(
            request, "partials/chat/choice_card.html", {"card": ChoiceCard(skipped)}
        )


def build_clarifications_router(ctx: ChatContext) -> APIRouter:
    router = APIRouter()
    routes = _ClarificationRoutes(ctx)
    router.post("/clarifications/{clarification_id}/answer", response_class=HTMLResponse)(
        routes.answer
    )
    router.post("/clarifications/{clarification_id}/abandon", response_class=HTMLResponse)(
        routes.abandon
    )
    return router
