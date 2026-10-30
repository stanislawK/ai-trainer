"""A sport's chat cards (#255, #256): goals, level and schedule, asked about that sport, with
Skip and Back, at `/onboarding/<sport_id>/<step>`."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from ai_trainer.application.ai_onboarding import (
    SPORT_STEPS,
    EmptyAnswerError,
    OnboardingCard,
    Place,
    answer_of,
    send_answer,
    skip_step,
)
from ai_trainer.domain.onboarding import MAX_ANSWER_LENGTH, OnboardingStep
from ai_trainer.domain.users import User
from ai_trainer.web.ai_onboarding.context import UNPROCESSABLE, OnboardingContext, go, path


def _progress(step: OnboardingStep, *, first: bool) -> tuple[int, int]:
    """A sport step's place in its round, counted from 0, and the round's length (Onboarding
    v4): the first sport's round is the sport, About you and its three steps; a later one's is
    "Add another sport?" and its three steps."""
    index = SPORT_STEPS.index(step)
    return (2 + index, 5) if first else (1 + index, 4)


class _SportStepRoutes:
    def __init__(self, ctx: OnboardingContext, step: OnboardingStep) -> None:
        self._ctx = ctx
        self._step = step
        self._card = OnboardingCard(step.value)

    async def _render(
        self,
        request: Request,
        place: Place,
        text: str,
        *,
        error: bool = False,
        status_code: int = 200,
    ) -> HTMLResponse:
        ctx = self._ctx
        user: User = request.state.user
        sport_id = place.sport_id or ""
        index, steps = _progress(
            self._step, first=sport_id == (await ctx.answers.list_sports(user.id))[0]
        )
        return ctx.render(
            request,
            "sport_step",
            {
                "step": self._step.value,
                "place": index,
                "round_length": steps,
                "back_href": path(await ctx.beside(user, place, -1)),
                "url": path(place),
                "sport": ctx.registry.get(sport_id),
                "text": text,
                "error": error,
                "max_length": MAX_ANSWER_LENGTH,
            },
            status_code=status_code,
        )

    async def show(self, request: Request, sport_id: str) -> Response:
        ctx = self._ctx
        place = Place(self._card, sport_id)
        if (elsewhere := await ctx.misplaced(request, place)) is not None:
            return elsewhere
        user: User = request.state.user
        earlier = await answer_of(user.id, self._step, answers=ctx.answers, sport_id=sport_id)
        return await self._render(request, place, earlier.text or "" if earlier else "")

    async def send(self, request: Request, sport_id: str) -> Response:
        ctx = self._ctx
        place = Place(self._card, sport_id)
        if (elsewhere := await ctx.misplaced(request, place)) is not None:
            return elsewhere
        user: User = request.state.user
        text = (await request.form()).get("answer")
        typed = text if isinstance(text, str) else ""
        try:
            await send_answer(
                user.id,
                self._step,
                typed,
                answers=ctx.answers,
                reads=ctx.reads,
                reader=ctx.reader,
                sport_id=sport_id,
                timezone=user.timezone,
            )
        except EmptyAnswerError:
            return await self._render(request, place, typed, error=True, status_code=UNPROCESSABLE)
        return go(request, path(await ctx.beside(user, place, 1)))

    async def skip(self, request: Request, sport_id: str) -> Response:
        ctx = self._ctx
        place = Place(self._card, sport_id)
        if (elsewhere := await ctx.misplaced(request, place)) is not None:
            return elsewhere
        user: User = request.state.user
        await skip_step(user.id, self._step, answers=ctx.answers, sport_id=sport_id)
        return go(request, path(await ctx.beside(user, place, 1)))


def build_sport_steps_router(ctx: OnboardingContext) -> APIRouter:
    router = APIRouter(prefix="/onboarding")
    for step in SPORT_STEPS:
        routes = _SportStepRoutes(ctx, step)
        at = f"/{{sport_id}}/{step.value}"
        router.add_api_route(at, routes.show, methods=["GET"], name=f"onboarding_{step.value}_card")
        router.add_api_route(
            at, routes.send, methods=["POST"], name=f"onboarding_{step.value}_send"
        )
        router.add_api_route(
            f"{at}/skip", routes.skip, methods=["POST"], name=f"onboarding_{step.value}_skip"
        )
    return router
