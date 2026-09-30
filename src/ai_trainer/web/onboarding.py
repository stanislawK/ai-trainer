from collections.abc import Mapping, Sequence
from itertools import zip_longest
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.onboarding import (
    MAX_GOAL_LENGTH,
    WEEKDAYS,
    EmptySportSelectionError,
    GoalProblem,
    InvalidAvailabilityError,
    InvalidGoalsError,
    NoAvailabilityError,
    NoGoalsError,
    choose_sports,
    earliest_today,
    set_availability,
    set_goals,
)
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.users import User

_NEXT_STEP_PATH = "/onboarding/availability"
_GOALS_STEP_PATH = "/onboarding/goals"
_TIMEZONE_STEP_PATH = "/onboarding/timezone"
_UNPROCESSABLE = 422


def build_onboarding_router(
    templates: Jinja2Templates,
    *,
    registry: SportRegistry,
    onboarding: OnboardingRepositoryPort,
    clock: ClockPort,
) -> APIRouter:
    """Wires the onboarding steps (PRD-0003 F6, ADR-0006, tickets #74-#76): sports,
    availability and goals. `user_id` comes only from the authenticated session (ADR-0005)."""
    router = APIRouter(prefix="/onboarding")

    def render(
        request: Request, page: str, context: dict[str, object], *, status_code: int = 200
    ) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        prefix = "partials" if is_htmx else "pages"
        return templates.TemplateResponse(
            request, f"{prefix}/onboarding/{page}.html", context, status_code=status_code
        )

    @router.get("/sports", response_class=HTMLResponse)
    async def sports_step(request: Request) -> HTMLResponse:
        user: User = request.state.user
        selected = await onboarding.list_sports(user.id)
        return render(request, "sports", {"sports": registry.all(), "selected": selected})

    @router.post("/sports")
    async def choose_sports_step(
        request: Request, sports: Annotated[list[str] | None, Form()] = None
    ) -> Response:
        user: User = request.state.user
        picked = sports or []
        try:
            await choose_sports(user.id, picked, registry=registry, repository=onboarding)
        except UnknownSportError as exc:
            raise HTTPException(status_code=_UNPROCESSABLE, detail=str(exc)) from exc
        except EmptySportSelectionError:
            return render(
                request,
                "sports",
                {"sports": registry.all(), "selected": [], "error": True},
                status_code=_UNPROCESSABLE,
            )

        return next_step(request, _NEXT_STEP_PATH)

    def next_step(request: Request, path: str) -> Response:
        # htmx never processes redirect headers on a 3xx, so an htmx request gets
        # `HX-Redirect` on a 2xx instead.
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=200, headers={"HX-Redirect": path})
        return RedirectResponse(url=path, status_code=303)

    @router.get("/availability", response_class=HTMLResponse)
    async def availability_step(request: Request) -> HTMLResponse:
        user: User = request.state.user
        saved = await onboarding.list_availability(user.id)
        minutes = {weekday: str(saved.get(weekday, 0)) for weekday in WEEKDAYS}
        return render(request, "availability", {"minutes": minutes, "invalid": frozenset()})

    @router.post("/availability")
    async def set_availability_step(request: Request) -> Response:
        user: User = request.state.user
        form = await request.form()
        typed = {
            weekday: value if isinstance(value := form.get(f"minutes_{weekday}", ""), str) else ""
            for weekday in WEEKDAYS
        }
        try:
            await set_availability(user.id, typed, repository=onboarding)
        except (InvalidAvailabilityError, NoAvailabilityError) as exc:
            invalid = exc.weekdays if isinstance(exc, InvalidAvailabilityError) else frozenset()
            return render(
                request,
                "availability",
                {"minutes": typed, "invalid": invalid, "no_days": not invalid},
                status_code=_UNPROCESSABLE,
            )
        return next_step(request, _GOALS_STEP_PATH)

    def render_goals(
        request: Request,
        rows: Sequence[tuple[str, str]],
        *,
        problems: Mapping[int, GoalProblem] | None = None,
        no_goals: bool = False,
        status_code: int = 200,
    ) -> HTMLResponse:
        return render(
            request,
            "goals",
            {
                # An empty row to start from, so the athlete never faces a blank card.
                "rows": list(rows) or [("", "")],
                "problems": problems or {},
                "no_goals": no_goals,
                "min_date": earliest_today(clock).isoformat(),
                "max_length": MAX_GOAL_LENGTH,
            },
            status_code=status_code,
        )

    @router.get("/goals", response_class=HTMLResponse)
    async def goals_step(request: Request) -> HTMLResponse:
        user: User = request.state.user
        saved = await onboarding.list_goals(user.id)
        rows = [
            (goal.text, goal.target_date.isoformat() if goal.target_date else "") for goal in saved
        ]
        return render_goals(request, rows)

    @router.post("/goals")
    async def set_goals_step(request: Request) -> Response:
        user: User = request.state.user
        form = await request.form()
        texts = [value for value in form.getlist("goal_text") if isinstance(value, str)]
        dates = [value for value in form.getlist("goal_date") if isinstance(value, str)]
        # Each row posts one text and one date; a row missing its date field is undated.
        rows = [
            (text, raw_date or "")
            for text, raw_date in zip_longest(texts, dates)
            if text is not None
        ]
        try:
            await set_goals(user.id, rows, clock=clock, repository=onboarding)
        except NoGoalsError:
            return render_goals(request, [], no_goals=True, status_code=_UNPROCESSABLE)
        except InvalidGoalsError as exc:
            return render_goals(request, rows, problems=exc.problems, status_code=_UNPROCESSABLE)
        return next_step(request, _TIMEZONE_STEP_PATH)

    return router
