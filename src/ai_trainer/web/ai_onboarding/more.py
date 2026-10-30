""" "Add another sport?" (#256): another sport opens its goals card, and "No, show my summary"
goes on to the summary."""

from fastapi import APIRouter, HTTPException, Request, Response

from ai_trainer.application.ai_onboarding import (
    AlreadyPickedError,
    OnboardingCard,
    Place,
    add_sport,
)
from ai_trainer.domain.sports.registry import UnknownSportError
from ai_trainer.domain.users import User
from ai_trainer.web.ai_onboarding.context import MORE, UNPROCESSABLE, OnboardingContext, go, path


class _MoreRoutes:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    async def more_card(self, request: Request) -> Response:
        """ "Add another sport?" (#256): the sports not picked yet, and the way to the summary."""
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, MORE)) is not None:
            return elsewhere
        user: User = request.state.user
        picked = await ctx.answers.list_sports(user.id)
        return ctx.render(
            request,
            "more",
            {
                "sports": [sport for sport in ctx.registry.all() if sport.id not in picked],
                "back_href": path(await ctx.beside(user, MORE, -1)),
            },
        )

    async def choose_another_sport(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, MORE)) is not None:
            return elsewhere
        user: User = request.state.user
        sport = (await request.form()).get("sport")
        sport_id = sport if isinstance(sport, str) else ""
        try:
            await add_sport(user.id, sport_id, registry=ctx.registry, answers=ctx.answers)
        except (UnknownSportError, AlreadyPickedError) as exc:
            raise HTTPException(status_code=UNPROCESSABLE, detail=str(exc)) from exc
        return go(request, path(Place(OnboardingCard.GOALS, sport_id)))


def build_more_router(ctx: OnboardingContext) -> APIRouter:
    router = APIRouter(prefix="/onboarding")
    routes = _MoreRoutes(ctx)
    router.get("/more")(routes.more_card)
    router.post("/more")(routes.choose_another_sport)
    return router
