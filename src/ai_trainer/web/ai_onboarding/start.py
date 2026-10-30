"""`/onboarding`, which sends the athlete to the card that is due, and the sport card that opens
the flow with the first sport and the athlete's timezone (#254)."""

from fastapi import APIRouter, HTTPException, Request, Response

from ai_trainer.application.ai_onboarding import OnboardingCard, pick_sport
from ai_trainer.domain.sports.registry import UnknownSportError
from ai_trainer.domain.users import User
from ai_trainer.web.ai_onboarding.context import (
    CARD_PATHS,
    HOME_PATH,
    SPORT,
    UNPROCESSABLE,
    OnboardingContext,
    go,
    path,
)


class _StartRoutes:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    async def start(self, request: Request) -> Response:
        user: User = request.state.user
        if user.onboarded_at is not None:
            return go(request, HOME_PATH)
        return go(request, path(await self._ctx.due(user)))

    async def sport_card(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, SPORT)) is not None:
            return elsewhere
        user: User = request.state.user
        picked = await ctx.answers.list_sports(user.id)
        return ctx.render(
            request,
            "sport",
            {"sports": ctx.registry.all(), "picked": picked[0] if picked else None},
        )

    async def choose_sport(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, SPORT)) is not None:
            return elsewhere
        user: User = request.state.user
        form = await request.form()
        sport = form.get("sport")
        zone = form.get("timezone")
        try:
            await pick_sport(
                user.id,
                sport if isinstance(sport, str) else "",
                zone if isinstance(zone, str) else "",
                registry=ctx.registry,
                answers=ctx.answers,
            )
        except UnknownSportError as exc:
            raise HTTPException(status_code=UNPROCESSABLE, detail=str(exc)) from exc
        return go(request, CARD_PATHS[OnboardingCard.ABOUT_YOU])


def build_start_router(ctx: OnboardingContext) -> APIRouter:
    router = APIRouter(prefix="/onboarding")
    routes = _StartRoutes(ctx)
    router.get("")(routes.start)
    router.get("/sport")(routes.sport_card)
    router.post("/sport")(routes.choose_sport)
    return router
