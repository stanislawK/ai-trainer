from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.onboarding import EmptySportSelectionError, choose_sports
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.users import User

_NEXT_STEP_PATH = "/onboarding/availability"
_UNPROCESSABLE = 422


def build_onboarding_router(
    templates: Jinja2Templates, *, registry: SportRegistry, onboarding: OnboardingRepositoryPort
) -> APIRouter:
    """Wires the onboarding steps (PRD-0003 F6, ADR-0006, ticket #74): the sports step, and
    a placeholder for step 2 until the availability step lands. `user_id` comes only from the
    authenticated session (ADR-0005)."""
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

        # htmx never processes redirect headers on a 3xx, so an htmx request gets
        # `HX-Redirect` on a 2xx instead (same shape as `/settings/delete-account`).
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=200, headers={"HX-Redirect": _NEXT_STEP_PATH})
        return RedirectResponse(url=_NEXT_STEP_PATH, status_code=303)

    @router.get("/availability", response_class=HTMLResponse)
    async def availability_step(request: Request) -> HTMLResponse:
        return render(request, "availability", {})

    return router
