"""The admin's review of exercise media (F24, ADR-0031 *Media links*, #554; the Exercise Media
mock): `GET /admin/exercises?view=suggested|approved` lists the pictures per exercise,
`POST /admin/exercises/links/{link_id}/approve` approves one, and
`POST /admin/exercises/{exercise_id}/approve-wger` approves the exercise's suggested wger pictures
the page drew, posted back as `link_id` fields, never a generated one nor one imported since.
Every response renders the whole `#exercise-media` region.

As on the other admin pages, the admin check comes before anything from the request is parsed,
so a non-admin gets 403, never a 422 or 404. CSRF is checked by `CsrfMiddleware` before this.
Every response is `no-store`."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData
from starlette.templating import Jinja2Templates

from ai_trainer.application.exercise_media_review import (
    Approved,
    ApproveOutcome,
    BulkApproved,
    ExerciseMediaReview,
    NotSuggested,
)
from ai_trainer.domain.media import MediaLinkStatus
from ai_trainer.web.admin.access import forbidden, is_admin, is_htmx, not_found

# The filters this page builds; Removed comes with the removal ticket (#556).
_VIEWS = {"suggested": MediaLinkStatus.SUGGESTED, "approved": MediaLinkStatus.APPROVED}
# The bulk form posts one `link_id` per suggested wger picture on one exercise; far more than any
# exercise carries, and a flood is refused by the form parser (400).
_MAX_BULK_FIELDS = 200


def _uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


def _link_ids(form: FormData) -> list[UUID] | None:
    """Every posted link ID, or None when any of them isn't one."""
    ids = [_uuid(value) if isinstance(value, str) else None for value in form.getlist("link_id")]
    return None if any(i is None for i in ids) else [i for i in ids if i is not None]


def _kind(outcome: ApproveOutcome | BulkApproved | None) -> str | None:
    """Which toast the template draws."""
    match outcome:
        case Approved():
            return "approved"
        case NotSuggested():
            return "not_suggested"
        case BulkApproved():
            return "bulk"
        case None:
            return None


class _ExerciseMediaRoutes:
    def __init__(self, *, templates: Jinja2Templates, review: ExerciseMediaReview) -> None:
        self._templates = templates
        self._review = review

    async def _region(
        self,
        request: Request,
        view: MediaLinkStatus,
        *,
        status_code: int = 200,
        outcome: ApproveOutcome | BulkApproved | None = None,
    ) -> HTMLResponse:
        context = {
            "review": await self._review.page(view),
            "outcome": outcome,
            "outcome_kind": _kind(outcome),
            "views": tuple(_VIEWS.items()),
        }
        template_name = (
            "partials/admin/exercises.html" if is_htmx(request) else "pages/admin/exercises.html"
        )
        response = self._templates.TemplateResponse(
            request, template_name, context, status_code=status_code
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def page(self, request: Request) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        view = _VIEWS.get(request.query_params.get("view", ""), MediaLinkStatus.SUGGESTED)
        return await self._region(request, view)

    async def approve(self, request: Request, link_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _uuid(link_id)
        outcome = None if parsed is None else await self._review.approve(parsed)
        if outcome is None:
            return not_found(request, self._templates)
        status_code = 409 if isinstance(outcome, NotSuggested) else 200
        return await self._region(
            request, MediaLinkStatus.SUGGESTED, status_code=status_code, outcome=outcome
        )

    async def approve_wger(self, request: Request, exercise_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _uuid(exercise_id)
        async with request.form(max_files=0, max_fields=_MAX_BULK_FIELDS) as form:
            link_ids = _link_ids(form)
        if parsed is None or link_ids is None:
            return not_found(request, self._templates)
        outcome = await self._review.approve_all_from_wger(parsed, link_ids)
        return await self._region(request, MediaLinkStatus.SUGGESTED, outcome=outcome)


def build_exercises_router(*, templates: Jinja2Templates, review: ExerciseMediaReview) -> APIRouter:
    routes = _ExerciseMediaRoutes(templates=templates, review=review)
    router = APIRouter()
    router.add_api_route(
        "/admin/exercises", routes.page, methods=["GET"], response_class=HTMLResponse
    )
    router.add_api_route(
        "/admin/exercises/links/{link_id}/approve",
        routes.approve,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/admin/exercises/{exercise_id}/approve-wger",
        routes.approve_wger,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    return router
