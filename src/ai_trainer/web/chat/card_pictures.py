"""The exercise card's drawn picture (ADR-0031 *Picture on the card*; #641 option B): the slot
that draws a missing picture (#643) and "Looks wrong" on a picture the card drew (#644). Neither
answers the question. Both have the how-to routes' guards: another user's or an unknown question
or option, or a missing service, is a 404, an answered question a 409."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import NOT_FOUND, ChatContext, parse_id


class _CardPictureRoutes:
    def __init__(self, ctx: ChatContext) -> None:
        self._ctx = ctx

    async def picture(
        self, request: Request, clarification_id: str, option_id: str
    ) -> HTMLResponse:
        """A drawable candidate's loading picture slot (#643): draws its picture under the
        athlete whose card asked and returns it with its credit, or, on any fallback, nothing,
        which leaves the text-only row."""
        user: User = request.state.user
        question, index = await self._ctx.candidate(request, clarification_id, option_id)
        pictures = self._ctx.card_pictures
        if pictures is None:
            raise HTTPException(status_code=NOT_FOUND)
        picture = await pictures.load(user_id=user.id, exercise_id=parse_id(option_id))
        return self._ctx.templates.TemplateResponse(
            request,
            "partials/chat/card_picture.html",
            {
                "question": question,
                "option": question.clarification.options[index],
                "picture": None if picture is None else self._ctx.flaggable(picture),
            },
        )

    async def flag(self, request: Request, clarification_id: str, option_id: str) -> HTMLResponse:
        """ "Looks wrong" on a picture drawn on the card (ADR-0031 *Flag*, #644): takes it down
        for every athlete, records one content-free `picture_flagged` event, and returns the
        candidate as the card draws it now, with the thanks line once it shows no picture. A
        picture the card didn't draw is left as it is, and its candidate comes back unchanged;
        an already flagged one gets the same answer as the first tap and records nothing."""
        ctx = self._ctx
        user: User = request.state.user
        question, index = await ctx.candidate(request, clarification_id, option_id)
        flags = ctx.card_picture_flag
        if flags is None:
            raise HTTPException(status_code=NOT_FOUND)
        await flags.flag(user_id=user.id, exercise_id=parse_id(option_id))
        option = question.clarification.options[index]
        picture = (await ctx.exercise_pictures([question])).get(option.id)
        return ctx.templates.TemplateResponse(
            request,
            "partials/chat/picture_flagged_candidate.html",
            {
                "question": question,
                "option": option,
                "index": index + 1,
                "picture": picture,
                "howto": (await ctx.exercise_howtos([question])).get(option.id),
                "picture_flagged": picture is None,
            },
        )


def build_card_pictures_router(ctx: ChatContext) -> APIRouter:
    router = APIRouter()
    routes = _CardPictureRoutes(ctx)
    router.post(
        "/clarifications/{clarification_id}/picture/{option_id}", response_class=HTMLResponse
    )(routes.picture)
    router.post(
        "/clarifications/{clarification_id}/picture/{option_id}/flag",
        response_class=HTMLResponse,
    )(routes.flag)
    return router
