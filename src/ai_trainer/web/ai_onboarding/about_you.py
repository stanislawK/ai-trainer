"""The About you chat card (#254): its answer, read in the background, and its Skip."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from ai_trainer.application.ai_onboarding import EmptyAnswerError, answer_of, send_answer, skip_step
from ai_trainer.domain.onboarding import MAX_ANSWER_LENGTH, OnboardingStep
from ai_trainer.domain.users import User
from ai_trainer.web.ai_onboarding.context import (
    ABOUT_YOU,
    UNPROCESSABLE,
    OnboardingContext,
    go,
    path,
)


class _AboutYouRoutes:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    def _render(
        self, request: Request, text: str, *, error: bool = False, status_code: int = 200
    ) -> HTMLResponse:
        return self._ctx.render(
            request,
            "about_you",
            {"text": text, "error": error, "max_length": MAX_ANSWER_LENGTH},
            status_code=status_code,
        )

    async def about_you_card(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        earlier = await answer_of(user.id, OnboardingStep.ABOUT_YOU, answers=ctx.answers)
        return self._render(request, earlier.text or "" if earlier is not None else "")

    async def answer_about_you(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        form = await request.form()
        text = form.get("answer")
        typed = text if isinstance(text, str) else ""
        sports = await ctx.answers.list_sports(user.id)
        try:
            await send_answer(
                user.id,
                OnboardingStep.ABOUT_YOU,
                typed,
                answers=ctx.answers,
                reads=ctx.reads,
                reader=ctx.reader,
                sport_id=sports[0],
                timezone=user.timezone,
            )
        except EmptyAnswerError:
            return self._render(request, typed, error=True, status_code=UNPROCESSABLE)
        return go(request, path(await ctx.beside(user, ABOUT_YOU, 1)))

    async def skip_about_you(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        await skip_step(user.id, OnboardingStep.ABOUT_YOU, answers=ctx.answers)
        return go(request, path(await ctx.beside(user, ABOUT_YOU, 1)))


def build_about_you_router(ctx: OnboardingContext) -> APIRouter:
    router = APIRouter(prefix="/onboarding")
    routes = _AboutYouRoutes(ctx)
    router.get("/about-you")(routes.about_you_card)
    router.post("/about-you")(routes.answer_about_you)
    router.post("/about-you/skip")(routes.skip_about_you)
    return router
