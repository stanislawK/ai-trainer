from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.onboarding import (
    WEEKDAYS,
    InvalidAvailabilityError,
    InvalidTimezoneError,
    NoAvailabilityError,
    confirm_timezone,
    set_availability,
    timezone_names,
)
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.users import User

_NEXT_STEP_PATH = "/onboarding/availability"
_TIMEZONE_STEP_PATH = "/onboarding/timezone"
_UNPROCESSABLE = 422
_HOME_PATH = "/"
_DEFAULT_TIMEZONE = "UTC"


def build_onboarding_router(
    templates: Jinja2Templates,
    *,
    registry: SportRegistry,
    onboarding: OnboardingRepositoryPort,
    clock: ClockPort,
) -> APIRouter:
    """Wires the form onboarding's later steps (PRD-0003 F6, ADR-0006, tickets #75 and #77):
    availability and the timezone. The AI onboarding (ADR-0028, `web/ai_onboarding.py`)
    replaced its sport and goals steps (#254, #255); these go in #256. `user_id` comes only from
    the authenticated session (ADR-0005)."""
    router = APIRouter(prefix="/onboarding")

    def render(
        request: Request, page: str, context: dict[str, object], *, status_code: int = 200
    ) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        prefix = "partials" if is_htmx else "pages"
        return templates.TemplateResponse(
            request, f"{prefix}/onboarding/{page}.html", context, status_code=status_code
        )

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
        return next_step(request, _TIMEZONE_STEP_PATH)

    def render_timezone(request: Request, *, error: bool = False) -> HTMLResponse:
        return render(
            request,
            "timezone",
            {"zones": timezone_names(), "selected": _DEFAULT_TIMEZONE, "error": error},
            status_code=_UNPROCESSABLE if error else 200,
        )

    @router.get("/timezone")
    async def timezone_step(request: Request) -> Response:
        user: User = request.state.user
        if user.onboarded_at:
            return next_step(request, _HOME_PATH)
        return render_timezone(request)

    @router.post("/timezone")
    async def confirm_timezone_step(request: Request) -> Response:
        user: User = request.state.user
        form = await request.form()
        zone = form.get("timezone", "")
        try:
            await confirm_timezone(
                user.id, zone if isinstance(zone, str) else "", clock=clock, repository=onboarding
            )
        except InvalidTimezoneError:
            return render_timezone(request, error=True)
        return next_step(request, _HOME_PATH)

    return router
