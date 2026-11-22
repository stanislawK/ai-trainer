"""The admin's review of exercise media (F24, ADR-0031 *Media links*, #554; the Exercise Media
mock): `GET /admin/exercises?view=suggested|approved` lists the pictures per exercise,
`POST /admin/exercises/links/{link_id}/approve` approves one, and
`POST /admin/exercises/{exercise_id}/approve-wger` approves the exercise's suggested wger pictures
the page drew, posted back as `link_id` fields, never a generated one nor one imported since.
`POST /admin/exercises/links/{link_id}/remove` hides a picture at once and `.../restore` puts it
back (#556); `?view=removed` lists the removed ones. Remove and Restore redraw the filter named in
their own `?view=`. `?view=howtos` lists the flagged and `rejected` beginner how-tos instead
(ADR-0031, #594): `&edit=<exercise_id>` draws that card's text as a field,
`POST /admin/exercises/howtos/{exercise_id}/edit` stores the admin's text (422, redrawn in edit
mode with the text kept, when it is empty or over the cap), and `.../clear` deletes the row.
Every response renders the whole `#exercise-media` region.

As on the other admin pages, the admin check comes before anything from the request is parsed,
so a non-admin gets 403, never a 422 or 404. CSRF is checked by `CsrfMiddleware` before this.
Every response is `no-store`."""

from dataclasses import dataclass
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
    Removed,
    Restored,
    RestoreFailed,
    RestoreOutcome,
)
from ai_trainer.application.exercise_simple_howto import (
    HowtoCleared,
    HowtoEdited,
    HowtoEditInvalid,
    HowtoReview,
)
from ai_trainer.domain.media import MediaLinkStatus
from ai_trainer.web.admin.access import forbidden, is_admin, is_htmx, not_found

# The filters this page builds, in the order the chips show them.
_VIEWS = {
    "suggested": MediaLinkStatus.SUGGESTED,
    "approved": MediaLinkStatus.APPROVED,
    "removed": MediaLinkStatus.REMOVED,
}
# The how-tos list's filter, the chip after the picture filters.
_HOWTOS = "howtos"
# The bulk form posts one `link_id` per suggested wger picture on one exercise; far more than any
# exercise carries, and a flood is refused by the form parser (400).
_MAX_BULK_FIELDS = 200
# The how-to edit posts one `text` field.
_MAX_HOWTO_FIELDS = 4


def _uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


def _link_ids(form: FormData) -> list[UUID] | None:
    """Every posted link ID, or None when any of them isn't one."""
    ids = [_uuid(value) if isinstance(value, str) else None for value in form.getlist("link_id")]
    return None if any(i is None for i in ids) else [i for i in ids if i is not None]


type _Outcome = ApproveOutcome | BulkApproved | Removed | RestoreOutcome | HowtoCleared


# The toast each outcome draws, but a removal's, which depends on what happened to its file.
_KINDS: dict[type, str] = {
    Approved: "approved",
    NotSuggested: "not_suggested",
    BulkApproved: "bulk",
    Restored: "restored",
    RestoreFailed: "restore_failed",
    HowtoCleared: "cleared",
}


def _kind(outcome: _Outcome | None) -> str | None:
    """Which toast the template draws."""
    match outcome:
        case Removed(shared=True):
            return "removed_shared"
        case Removed(still_public=True):
            return "still_public"
        case Removed():
            return "removed"
        case None:
            return None
        case _:
            return _KINDS[type(outcome)]


def _view(request: Request) -> MediaLinkStatus:
    return _VIEWS.get(request.query_params.get("view", ""), MediaLinkStatus.SUGGESTED)


@dataclass(frozen=True, slots=True)
class _Editing:
    """The how-to card drawn as a field: its exercise, and after a refused save, what the admin
    typed and why it was refused."""

    exercise_id: UUID
    text: str | None = None
    error: HowtoEditInvalid | None = None


class _ExerciseMediaRoutes:
    def __init__(
        self, *, templates: Jinja2Templates, review: ExerciseMediaReview, howtos: HowtoReview
    ) -> None:
        self._templates = templates
        self._review = review
        self._howtos = howtos

    async def _region(
        self,
        request: Request,
        view: MediaLinkStatus | None,
        *,
        status_code: int = 200,
        outcome: _Outcome | None = None,
        link_id: UUID | None = None,
        editing: _Editing | None = None,
    ) -> HTMLResponse:
        """`view` is a picture filter, or `None` for the how-tos list."""
        howtos = await self._howtos.rows()
        review = None if view is None else await self._review.page(view)
        counts = review.counts if review is not None else await self._review.counts()
        context = {
            # The link Undo, Restore and Try again post back to.
            "link_id": link_id,
            "review": review,
            "howtos": None if review is not None else howtos,
            "editing": editing,
            "outcome": outcome,
            "outcome_kind": _kind(outcome),
            "current": _HOWTOS if view is None else view.value,
            "chips": [(id_, counts[status]) for id_, status in _VIEWS.items()]
            + [(_HOWTOS, len(howtos))],
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
        if request.query_params.get("view") != _HOWTOS:
            return await self._region(request, _view(request))
        edit = _uuid(request.query_params.get("edit", ""))
        return await self._region(
            request, None, editing=None if edit is None else _Editing(exercise_id=edit)
        )

    async def edit_howto(self, request: Request, exercise_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _uuid(exercise_id)
        async with request.form(max_files=0, max_fields=_MAX_HOWTO_FIELDS) as form:
            typed = form.get("text")
        if parsed is None or not isinstance(typed, str):
            return not_found(request, self._templates)
        match await self._howtos.edit(parsed, typed):
            case HowtoEdited():
                return await self._region(request, None)
            case HowtoEditInvalid() as error:
                editing = _Editing(exercise_id=parsed, text=typed, error=error)
                return await self._region(request, None, status_code=422, editing=editing)
            case None:
                return not_found(request, self._templates)

    async def clear_howto(self, request: Request, exercise_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _uuid(exercise_id)
        outcome = None if parsed is None else await self._howtos.clear(parsed)
        if outcome is None:
            return not_found(request, self._templates)
        return await self._region(request, None, outcome=outcome)

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

    async def remove(self, request: Request, link_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _uuid(link_id)
        outcome = None if parsed is None else await self._review.remove(parsed)
        if outcome is None:
            return not_found(request, self._templates)
        return await self._region(request, _view(request), outcome=outcome, link_id=parsed)

    async def restore(self, request: Request, link_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _uuid(link_id)
        outcome = None if parsed is None else await self._review.restore(parsed)
        if outcome is None:
            return not_found(request, self._templates)
        return await self._region(request, _view(request), outcome=outcome, link_id=parsed)

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


def build_exercises_router(
    *, templates: Jinja2Templates, review: ExerciseMediaReview, howtos: HowtoReview
) -> APIRouter:
    routes = _ExerciseMediaRoutes(templates=templates, review=review, howtos=howtos)
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
        "/admin/exercises/links/{link_id}/remove",
        routes.remove,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/admin/exercises/links/{link_id}/restore",
        routes.restore,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/admin/exercises/{exercise_id}/approve-wger",
        routes.approve_wger,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/admin/exercises/howtos/{exercise_id}/edit",
        routes.edit_howto,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/admin/exercises/howtos/{exercise_id}/clear",
        routes.clear_howto,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    return router
