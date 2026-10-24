"""The AI onboarding's routes (PRD-0007 F6, F20, F21, ADR-0028, ticket #254): the sport card, the
About you chat card with Skip and Back, the summary that catches up on reads over SSE, and its
confirm. No request here awaits a model call except the summary stream (invariant 2), and
`user_id` comes only from the authenticated session (ADR-0005)."""

from collections.abc import AsyncIterable, Mapping

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.templating import Jinja2Templates

from ai_trainer.application.ai_onboarding import (
    EmptyAnswerError,
    InvalidSummaryError,
    OnboardingCard,
    OnboardingSummary,
    answer_of,
    catch_up,
    confirm_summary,
    current_card,
    pick_sport,
    send_answer,
    skip_step,
    summary_of,
)
from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.application.onboarding import timezone_names
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingAnswersPort, OnboardingReaderPort
from ai_trainer.domain.athlete_profile import MAX_LIMITS_LENGTH, FieldProblem
from ai_trainer.domain.onboarding import MAX_ANSWER_LENGTH, OnboardingStep
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.users import User

_UNPROCESSABLE = 422
_HOME_PATH = "/"
_CARD_PATHS = {
    OnboardingCard.SPORT: "/onboarding/sport",
    OnboardingCard.ABOUT_YOU: "/onboarding/about-you",
    OnboardingCard.SUMMARY: "/onboarding/summary",
}
# The fields the summary posts, as typed.
_SUMMARY_FIELDS = ("age", "height_cm", "weight_kg", "limits", "timezone")


def build_ai_onboarding_router(
    templates: Jinja2Templates,
    *,
    registry: SportRegistry,
    answers: OnboardingAnswersPort,
    reads: BackgroundReads,
    reader: OnboardingReaderPort,
    clock: ClockPort,
) -> APIRouter:
    router = APIRouter(prefix="/onboarding")

    def render(
        request: Request, page: str, context: Mapping[str, object], *, status_code: int = 200
    ) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        prefix = "partials" if is_htmx else "pages"
        return templates.TemplateResponse(
            request, f"{prefix}/onboarding/{page}.html", dict(context), status_code=status_code
        )

    def go(request: Request, path: str) -> Response:
        # htmx never processes redirect headers on a 3xx, so an htmx request gets
        # `HX-Redirect` on a 2xx instead.
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=200, headers={"HX-Redirect": path})
        return RedirectResponse(url=path, status_code=303)

    async def misplaced(request: Request, card: OnboardingCard) -> Response | None:
        """Where to send an athlete who may not be on `card` yet, or at all: home once
        onboarded, else the card the server says comes next when that is an earlier one."""
        user: User = request.state.user
        if user.onboarded_at is not None:
            return go(request, _HOME_PATH)
        due = await current_card(user.id, answers=answers)
        order = list(OnboardingCard)
        if order.index(due) < order.index(card):
            return go(request, _CARD_PATHS[due])
        return None

    @router.get("")
    async def start(request: Request) -> Response:
        user: User = request.state.user
        if user.onboarded_at is not None:
            return go(request, _HOME_PATH)
        return go(request, _CARD_PATHS[await current_card(user.id, answers=answers)])

    @router.get("/sport")
    async def sport_card(request: Request) -> Response:
        if (elsewhere := await misplaced(request, OnboardingCard.SPORT)) is not None:
            return elsewhere
        user: User = request.state.user
        picked = await answers.list_sports(user.id)
        return render(
            request, "sport", {"sports": registry.all(), "picked": picked[0] if picked else None}
        )

    @router.post("/sport")
    async def choose_sport(request: Request) -> Response:
        if (elsewhere := await misplaced(request, OnboardingCard.SPORT)) is not None:
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
                registry=registry,
                answers=answers,
            )
        except UnknownSportError as exc:
            raise HTTPException(status_code=_UNPROCESSABLE, detail=str(exc)) from exc
        return go(request, _CARD_PATHS[OnboardingCard.ABOUT_YOU])

    def render_about_you(
        request: Request, text: str, *, error: bool = False, status_code: int = 200
    ) -> HTMLResponse:
        return render(
            request,
            "about_you",
            {"text": text, "error": error, "max_length": MAX_ANSWER_LENGTH},
            status_code=status_code,
        )

    @router.get("/about-you")
    async def about_you_card(request: Request) -> Response:
        if (elsewhere := await misplaced(request, OnboardingCard.ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        earlier = await answer_of(user.id, OnboardingStep.ABOUT_YOU, answers=answers)
        return render_about_you(request, earlier.text or "" if earlier is not None else "")

    @router.post("/about-you")
    async def answer_about_you(request: Request) -> Response:
        if (elsewhere := await misplaced(request, OnboardingCard.ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        form = await request.form()
        text = form.get("answer")
        typed = text if isinstance(text, str) else ""
        sports = await answers.list_sports(user.id)
        try:
            await send_answer(
                user.id,
                OnboardingStep.ABOUT_YOU,
                typed,
                answers=answers,
                reads=reads,
                reader=reader,
                sport_id=sports[0],
                timezone=user.timezone,
            )
        except EmptyAnswerError:
            return render_about_you(request, typed, error=True, status_code=_UNPROCESSABLE)
        return go(request, _CARD_PATHS[OnboardingCard.SUMMARY])

    @router.post("/about-you/skip")
    async def skip_about_you(request: Request) -> Response:
        if (elsewhere := await misplaced(request, OnboardingCard.ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        await skip_step(user.id, OnboardingStep.ABOUT_YOU, answers=answers)
        return go(request, _CARD_PATHS[OnboardingCard.SUMMARY])

    @router.get("/summary")
    async def summary_page(request: Request) -> Response:
        if (elsewhere := await misplaced(request, OnboardingCard.SUMMARY)) is not None:
            return elsewhere
        return render(request, "summary", {})

    def summary_context(
        summary: OnboardingSummary,
        *,
        values: Mapping[str, str] | None = None,
        problems: Mapping[str, FieldProblem] | None = None,
    ) -> dict[str, object]:
        about_you = summary.about_you
        shown = values or {
            "age": str(about_you.age) if about_you.age is not None else "",
            "height_cm": str(about_you.height_cm) if about_you.height_cm is not None else "",
            "weight_kg": f"{about_you.weight_kg:g}" if about_you.weight_kg is not None else "",
            "limits": about_you.limits or "",
            "timezone": summary.timezone,
        }
        return {
            "values": shown,
            "problems": problems or {},
            "skipped": summary.about_you_skipped,
            "zones": timezone_names(),
            "max_limits": MAX_LIMITS_LENGTH,
        }

    @router.get("/summary/stream", response_class=EventSourceResponse)
    async def stream_summary(request: Request) -> AsyncIterable[ServerSentEvent]:
        """Finishes every unread answer, then sends the editable summary in place of the
        skeleton (B35). The only onboarding request that waits on a model call."""
        user: User = request.state.user
        await catch_up(user.id, answers=answers, reads=reads, reader=reader, timezone=user.timezone)
        summary = await summary_of(user.id, answers=answers, timezone=user.timezone)
        html = templates.get_template("partials/onboarding/summary_ready.html").render(
            request=request,
            csrf_token=getattr(request.state, "csrf_token", None),
            **summary_context(summary),
        )
        yield ServerSentEvent(raw_data=html)

    @router.post("/confirm")
    async def confirm(request: Request) -> Response:
        if (elsewhere := await misplaced(request, OnboardingCard.SUMMARY)) is not None:
            return elsewhere
        user: User = request.state.user
        form = await request.form()
        typed = {
            field: value if isinstance(value := form.get(field, ""), str) else ""
            for field in _SUMMARY_FIELDS
        }
        try:
            await confirm_summary(user.id, typed, answers=answers, clock=clock)
        except InvalidSummaryError as exc:
            summary = await summary_of(user.id, answers=answers, timezone=user.timezone)
            return render(
                request,
                "summary_form",
                summary_context(summary, values=typed, problems=exc.problems),
                status_code=_UNPROCESSABLE,
            )
        return go(request, _HOME_PATH)

    return router
