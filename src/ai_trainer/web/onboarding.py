from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import zip_longest

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.onboarding import (
    MAX_GOAL_LENGTH,
    WEEKDAYS,
    DroppedSportsHaveGoalsError,
    EmptySportSelectionError,
    GoalProblem,
    InvalidAvailabilityError,
    InvalidGoalChoiceError,
    InvalidGoalsError,
    NoAvailabilityError,
    NoGoalsError,
    UnknownGoalSportError,
    choose_sports,
    earliest_today,
    set_availability,
    set_goals,
)
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.domain.sports.base import SportPlugin
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.users import User

_NEXT_STEP_PATH = "/onboarding/availability"
_GOALS_STEP_PATH = "/onboarding/goals"
_TIMEZONE_STEP_PATH = "/onboarding/timezone"
_UNPROCESSABLE = 422
_GOAL_CHOICE_PREFIX = "goals_"


@dataclass(frozen=True)
class GoalCard:
    """One card on the goals step: a picked sport, or General when `sport` is `None`. Each
    row is (index among the posted rows, text, target date)."""

    sport: SportPlugin | None
    rows: Sequence[tuple[int, str, str]]


def build_onboarding_router(
    templates: Jinja2Templates,
    *,
    registry: SportRegistry,
    onboarding: OnboardingRepositoryPort,
    clock: ClockPort,
) -> APIRouter:
    """Wires the onboarding steps (PRD-0003 F6, ADR-0006, tickets #74-#76 and #99): sports,
    availability and goals per sport. `user_id` comes only from the authenticated session
    (ADR-0005)."""
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
    async def choose_sports_step(request: Request) -> Response:
        user: User = request.state.user
        form = await request.form()
        picked = [value for value in form.getlist("sports") if isinstance(value, str)]
        # Only the dialog's Confirm carries `confirm_drop`; without it, dropping a sport that
        # has goals asks first, and with it every such sport needs its own valid choice.
        choices: dict[str, str] | None = None
        if form.get("confirm_drop") == "1":
            choices = {}
            for key in {key for key in form if key.startswith(_GOAL_CHOICE_PREFIX)}:
                values = form.getlist(key)
                # A repeated key would let a bad value hide behind a good one.
                if len(values) != 1 or not isinstance(values[0], str):
                    raise HTTPException(status_code=_UNPROCESSABLE, detail=f"one choice for {key}")
                choices[key.removeprefix(_GOAL_CHOICE_PREFIX)] = values[0]
        try:
            await choose_sports(
                user.id, picked, registry=registry, repository=onboarding, goal_choices=choices
            )
        except (UnknownSportError, InvalidGoalChoiceError) as exc:
            raise HTTPException(status_code=_UNPROCESSABLE, detail=str(exc)) from exc
        except EmptySportSelectionError:
            return render(
                request,
                "sports",
                {"sports": registry.all(), "selected": [], "error": True},
                status_code=_UNPROCESSABLE,
            )
        except DroppedSportsHaveGoalsError as exc:
            dropped = [
                (sport, exc.dropped[sport.id])
                for sport in registry.all()
                if sport.id in exc.dropped
            ]
            return render(
                request,
                "sports",
                {"sports": registry.all(), "selected": picked, "dropped": dropped},
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
        rows: Sequence[tuple[str, str, str]],
        picked: Sequence[str],
        *,
        problems: Mapping[int, GoalProblem] | None = None,
        no_goals: bool = False,
        status_code: int = 200,
    ) -> HTMLResponse:
        # One card per picked sport in registry order, then General. A row keeps its position
        # in `rows` as its index, so an error lands on the row it was found on. A row whose
        # sport has no card (a sport dropped since the goal was saved) joins General.
        sports = [sport for sport in registry.all() if sport.id in picked]
        by_sport: dict[str, list[tuple[int, str, str]]] = {sport.id: [] for sport in sports}
        by_sport[""] = []
        for index, (sport_id, text, raw_date) in enumerate(rows):
            by_sport[sport_id if sport_id in by_sport else ""].append((index, text, raw_date))
        cards = [
            GoalCard(sport=sport, rows=by_sport[sport.id] or [(-1, "", "")]) for sport in sports
        ]
        # An empty row to start from, so the athlete never faces a blank card.
        cards.append(GoalCard(sport=None, rows=by_sport[""] or [(-1, "", "")]))
        return render(
            request,
            "goals",
            {
                "cards": cards,
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
        picked = await onboarding.list_sports(user.id)
        saved = await onboarding.list_goals(user.id)
        rows = [
            (
                goal.sport_id or "",
                goal.text,
                goal.target_date.isoformat() if goal.target_date else "",
            )
            for goal in saved
        ]
        return render_goals(request, rows, picked)

    @router.post("/goals")
    async def set_goals_step(request: Request) -> Response:
        user: User = request.state.user
        picked = await onboarding.list_sports(user.id)
        form = await request.form()
        texts = [value for value in form.getlist("goal_text") if isinstance(value, str)]
        dates = [value for value in form.getlist("goal_date") if isinstance(value, str)]
        sport_ids = [value for value in form.getlist("goal_sport") if isinstance(value, str)]
        # Each row posts one sport, one text and one date; a row missing its sport is a
        # general goal and one missing its date is undated.
        rows = [
            (sport_id or "", text, raw_date or "")
            for text, raw_date, sport_id in zip_longest(texts, dates, sport_ids)
            if text is not None
        ]
        try:
            await set_goals(
                user.id,
                [(sport_id or None, text, raw_date) for sport_id, text, raw_date in rows],
                clock=clock,
                repository=onboarding,
            )
        except UnknownGoalSportError as exc:
            raise HTTPException(status_code=_UNPROCESSABLE, detail=str(exc)) from exc
        except NoGoalsError:
            return render_goals(request, rows, picked, no_goals=True, status_code=_UNPROCESSABLE)
        except InvalidGoalsError as exc:
            return render_goals(
                request, rows, picked, problems=exc.problems, status_code=_UNPROCESSABLE
            )
        return next_step(request, _TIMEZONE_STEP_PATH)

    return router
