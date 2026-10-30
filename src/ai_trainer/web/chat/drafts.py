"""A session draft's card and its actions: show, edit, confirm, discard, undo and the sport chip
(PRD-0004 F2, B4, B23, ADR-0022, tickets #154, #158, #163, #165 and #166). Each answers with the
card or the row it collapsed to, which replaces the one the athlete tapped."""

from collections.abc import Mapping

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ai_trainer.application.drafts import (
    confirm_draft,
    discard_draft,
    draft_to_edit,
    edit_draft,
    find_draft,
    undo_log,
)
from ai_trainer.application.reply import MessageNotFoundError, ReplyFailed, change_draft_sport
from ai_trainer.domain.training import (
    DraftAwaitingAnswerError,
    DraftConfirmedError,
    DraftDiscardedError,
    DraftEditError,
    DraftNotFoundError,
    DraftSportNotOfferedError,
    SessionNotFoundError,
)
from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import (
    CONFLICT,
    NOT_FOUND,
    UNPROCESSABLE,
    ChatContext,
    parse_id,
)
from ai_trainer.web.drafts import DraftCard, draft_card, form_changes


class _DraftRoutes:
    def __init__(self, ctx: ChatContext) -> None:
        self._ctx = ctx

    def _edit_form(
        self,
        request: Request,
        card: DraftCard,
        *,
        values: Mapping[str, str] | None = None,
        errors: frozenset[str] = frozenset(),
    ) -> HTMLResponse:
        return self._ctx.templates.TemplateResponse(
            request,
            "partials/chat/draft_edit.html",
            {"card": card, "values": values or {}, "errors": errors},
            status_code=UNPROCESSABLE if errors else 200,
        )

    async def show_draft(self, request: Request, draft_id: str) -> HTMLResponse:
        """The draft as it is stored: the pending card, or the row it collapsed to. The edit
        form's Cancel swaps it back in (ticket #158)."""
        ctx = self._ctx
        user: User = request.state.user
        try:
            draft = await find_draft(user.id, parse_id(draft_id), drafts=ctx.replies.drafts)
        except DraftNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        card = await ctx.shown_card(user, draft)
        return ctx.templates.TemplateResponse(
            request, card.template, {"card": card, "draft": draft}
        )

    async def open_edit(self, request: Request, draft_id: str) -> HTMLResponse:
        """The card's Edit, or its date chip (F2, ADR-0014 invariant 4): the pending draft's
        fields as an inline form, which replaces the card. A confirmed or discarded draft is a
        409."""
        ctx = self._ctx
        user: User = request.state.user
        try:
            draft = await draft_to_edit(user.id, parse_id(draft_id), drafts=ctx.replies.drafts)
        except DraftNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=CONFLICT) from None
        return self._edit_form(request, await ctx.shown_card(user, draft))

    async def save_edit(self, request: Request, draft_id: str) -> HTMLResponse:
        """The edit form's Save (F2): updates the pending draft and answers with its card,
        which the athlete then confirms as usual. A rejected value is a 422 with the form as
        typed and that field marked, and the draft unchanged. A confirmed or discarded draft
        is a 409."""
        ctx = self._ctx
        user: User = request.state.user
        parsed_id = parse_id(draft_id)
        form = await request.form()
        values = {key: value for key, value in form.multi_items() if isinstance(value, str)}
        try:
            draft = await edit_draft(
                user.id,
                parsed_id,
                form_changes(values.items()),
                drafts=ctx.replies.drafts,
                sports=ctx.sports,
            )
        except DraftNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=CONFLICT) from None
        except DraftEditError as error:
            stored = await find_draft(user.id, parsed_id, drafts=ctx.replies.drafts)
            return self._edit_form(
                request, await ctx.shown_card(user, stored), values=values, errors=error.fields
            )
        card = await ctx.shown_card(user, draft)
        return ctx.templates.TemplateResponse(
            request, card.template, {"card": card, "draft": draft}
        )

    async def confirm(self, request: Request, draft_id: str) -> HTMLResponse:
        """The draft card's Confirm (F2, ADR-0022): saves the athlete's draft once and answers
        with its "Logged … to <date>" row, which replaces the card. A second confirm answers
        with the same row. A discarded draft, or one whose card still asks whether it is the
        same session (#166), is a 409 and writes nothing."""
        ctx = self._ctx
        user: User = request.state.user
        try:
            draft = await confirm_draft(
                user.id, parse_id(draft_id), drafts=ctx.replies.drafts, clock=ctx.clock
            )
        except DraftNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except DraftDiscardedError, DraftAwaitingAnswerError:
            raise HTTPException(status_code=CONFLICT) from None
        card = draft_card(draft, sports=ctx.sports, today=ctx.local_today(user))
        return ctx.templates.TemplateResponse(
            request, "partials/chat/draft_confirmed.html", {"card": card, "draft": draft}
        )

    async def discard(self, request: Request, draft_id: str) -> HTMLResponse:
        """The draft card's Discard (F2, ADR-0022): throws the athlete's draft away without
        saving a session and answers with its "Draft discarded" row, which replaces the card.
        A second discard answers with the same row. A confirmed draft is a 409 and changes
        nothing."""
        ctx = self._ctx
        user: User = request.state.user
        try:
            draft = await discard_draft(
                user.id, parse_id(draft_id), drafts=ctx.replies.drafts, clock=ctx.clock
            )
        except DraftNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except DraftConfirmedError:
            raise HTTPException(status_code=CONFLICT) from None
        card = draft_card(draft, sports=ctx.sports, today=ctx.local_today(user))
        return ctx.templates.TemplateResponse(
            request, "partials/chat/draft_discarded.html", {"card": card, "draft": draft}
        )

    async def undo(self, request: Request, draft_id: str) -> HTMLResponse:
        """The confirmed row's Undo (B4, ADR-0022): takes the athlete's just-saved log back from
        its session, once the browser's prompt is accepted, and answers with the "Removed" row,
        which replaces the confirmed one. The session's other logs stay (#165). Another user's
        or an unknown draft, and one with no session left to undo, is a 404 and nothing is
        deleted."""
        ctx = self._ctx
        user: User = request.state.user
        try:
            draft = await undo_log(
                user.id, parse_id(draft_id), drafts=ctx.replies.drafts, clock=ctx.clock
            )
        except DraftNotFoundError, SessionNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        card = draft_card(draft, sports=ctx.sports, today=ctx.local_today(user))
        return ctx.templates.TemplateResponse(
            request, "partials/chat/draft_removed.html", {"card": card, "draft": draft}
        )

    async def change_sport(self, request: Request, draft_id: str) -> HTMLResponse:
        """The draft card's sport chip (B23, F2, ticket #163): re-runs the picked sport's
        extraction on the draft's own log and answers with the new card, which replaces the
        old one; no session is written. A sport the chip never offered is a 422, a confirmed or
        discarded draft a 409, and another user's or an unknown one a 404, none of them calling
        the model. A failed extraction answers with the card as it was and a note to pick
        again."""
        ctx = self._ctx
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
                services=ctx.replies,
            )
        except DraftNotFoundError, MessageNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except DraftConfirmedError, DraftDiscardedError:
            raise HTTPException(status_code=CONFLICT) from None
        except DraftSportNotOfferedError:
            raise HTTPException(status_code=UNPROCESSABLE) from None
        if isinstance(changed, ReplyFailed):
            card = await ctx.shown_card(
                user, await find_draft(user.id, parsed_id, drafts=ctx.replies.drafts)
            )
            return ctx.templates.TemplateResponse(
                request, card.template, {"card": card, "sport_error": True}
            )
        card = await ctx.shown_card(user, changed)
        return ctx.templates.TemplateResponse(request, card.template, {"card": card})


def build_drafts_router(ctx: ChatContext) -> APIRouter:
    router = APIRouter()
    routes = _DraftRoutes(ctx)
    router.get("/drafts/{draft_id}", response_class=HTMLResponse)(routes.show_draft)
    router.get("/drafts/{draft_id}/edit", response_class=HTMLResponse)(routes.open_edit)
    router.post("/drafts/{draft_id}/edit", response_class=HTMLResponse)(routes.save_edit)
    router.post("/drafts/{draft_id}/confirm", response_class=HTMLResponse)(routes.confirm)
    router.post("/drafts/{draft_id}/discard", response_class=HTMLResponse)(routes.discard)
    router.post("/drafts/{draft_id}/undo", response_class=HTMLResponse)(routes.undo)
    router.post("/drafts/{draft_id}/sport", response_class=HTMLResponse)(routes.change_sport)
    return router
