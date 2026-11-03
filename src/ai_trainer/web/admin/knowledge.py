"""The admin's knowledge sources (ADR-0033, PRD B7): `GET /admin/knowledge` lists them with the
upload sheet, `POST /admin/knowledge/sources` uploads one, and each source has `DELETE` and
`POST …/reingest`. Every response renders the whole `#knowledge` region, the list and the sheet,
so a refusal reopens the sheet and a success replaces the browser's Uploading row.

As on the users page, the admin check comes before anything from the request is parsed: the
upload's body and every source ID are read by hand after it, so a non-admin gets 403, never a
422 or 404."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData, UploadFile
from starlette.templating import Jinja2Templates

from ai_trainer.application.knowledge_sources import (
    AlreadyStored,
    KnowledgeSources,
    NotReingestable,
    ReingestQueued,
    SourceDeleted,
    SourceNotFound,
    SourceStored,
    StorageUnavailable,
    UploadOutcome,
    UploadRefused,
)
from ai_trainer.domain.knowledge import (
    UPLOAD_EVIDENCE_TIERS,
    UPLOAD_SOURCE_TYPES,
    KnowledgeSource,
    LicenseId,
    SourceLanguage,
)
from ai_trainer.domain.knowledge_form import DETECT_LICENCE, UploadAnswers
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.web.admin.access import forbidden, is_admin, is_htmx, not_found

# The upload's text fields; anything else posted is ignored.
_TEXT_FIELDS = (
    "title",
    "author",
    "source_type",
    "evidence_tier",
    "language",
    "topics",
    "license_id",
    "license_url",
    "copyright_statement",
    "attribution",
    "license_note",
    "source_url",
)
# The licence select's options, in its order: Detect from the file first (the #454 amendment).
_LICENCE_CHOICES = (DETECT_LICENCE, *(value.value for value in LicenseId))


@dataclass(frozen=True)
class _Sheet:
    """The upload sheet as the server renders it: open after a refusal or a notice, with the
    answers as typed, so nothing the admin wrote is lost."""

    open: bool = False
    answers: UploadAnswers = field(default_factory=UploadAnswers)
    problems: Mapping[str, str] = field(default_factory=dict)
    # What the notice says: `already` with `existing`, or `storage_not_configured` /
    # `storage_failed`. The words are in the template (G5).
    notice: str | None = None
    existing: KnowledgeSource | None = None
    file_name: str | None = None
    file_size: int | None = None


def _answers(form: FormData) -> UploadAnswers:
    texts = {name: form.get(name) for name in _TEXT_FIELDS}
    return UploadAnswers(
        **{name: value for name, value in texts.items() if isinstance(value, str)},
        sports=tuple(value for value in form.getlist("sports") if isinstance(value, str)),
    )


def _source_id(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


class _KnowledgeRoutes:
    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        knowledge: KnowledgeSources,
        sports: SportRegistry,
        max_upload_mb: int,
    ) -> None:
        self._templates = templates
        self._knowledge = knowledge
        self._sports = sports
        self._max_upload_mb = max_upload_mb

    async def _region(
        self,
        request: Request,
        *,
        status_code: int = 200,
        sheet: _Sheet | None = None,
        alert: str | None = None,
    ) -> HTMLResponse:
        context = {
            "sources": await self._knowledge.list(),
            "sheet": sheet or _Sheet(),
            "alert": alert,
            "source_types": UPLOAD_SOURCE_TYPES,
            "evidence_tiers": UPLOAD_EVIDENCE_TIERS,
            "languages": tuple(SourceLanguage),
            "licence_choices": _LICENCE_CHOICES,
            "sports": self._sports.all(),
            "max_upload_mb": self._max_upload_mb,
            "max_upload_bytes": self._max_upload_mb * 1024 * 1024,
        }
        template_name = (
            "partials/admin/knowledge.html" if is_htmx(request) else "pages/admin/knowledge.html"
        )
        return self._templates.TemplateResponse(
            request, template_name, context, status_code=status_code
        )

    async def page(self, request: Request) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        return await self._region(request)

    async def upload(self, request: Request) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        # Closing the form closes its spooled file.
        async with request.form(max_files=1, max_fields=len(_TEXT_FIELDS) + 8) as form:
            upload = form.get("file")
            file = upload if isinstance(upload, UploadFile) else None
            answers = _answers(form)
            sheet = _Sheet(
                open=True,
                answers=answers,
                file_name=file.filename if file is not None else None,
                file_size=file.size if file is not None else None,
            )
            outcome = await self._knowledge.upload(request.state.user.id, answers, file)
        return await self._uploaded(request, outcome, sheet)

    async def _uploaded(self, request: Request, outcome: UploadOutcome, sheet: _Sheet) -> Response:
        match outcome:
            case SourceStored():
                return await self._region(request)
            case UploadRefused(problems=problems):
                marked = {name.value: problem.value for name, problem in problems.items()}
                return await self._region(
                    request, status_code=422, sheet=replace(sheet, problems=marked)
                )
            case AlreadyStored(source=existing):
                notice = replace(sheet, notice="already", existing=existing)
                return await self._region(request, status_code=409, sheet=notice)
            case StorageUnavailable(reason=reason):
                failed = replace(sheet, notice=f"storage_{reason}")
                return await self._region(request, status_code=503, sheet=failed)

    async def delete(self, request: Request, source_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _source_id(source_id)
        if parsed is None:
            return not_found(request, self._templates)
        outcome = await self._knowledge.delete(parsed)
        match outcome:
            case SourceDeleted():
                return await self._region(request)
            case SourceNotFound():
                return not_found(request, self._templates)
            case StorageUnavailable(reason=reason):
                return await self._region(request, status_code=503, alert=f"delete_{reason}")

    async def reingest(self, request: Request, source_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        parsed = _source_id(source_id)
        if parsed is None:
            return not_found(request, self._templates)
        outcome = await self._knowledge.reingest(request.state.user.id, parsed)
        match outcome:
            case ReingestQueued():
                return await self._region(request)
            case NotReingestable():
                return await self._region(request, status_code=409, alert="reingest_waiting")
            case SourceNotFound():
                return not_found(request, self._templates)


def build_knowledge_router(
    *,
    templates: Jinja2Templates,
    knowledge: KnowledgeSources,
    sports: SportRegistry,
    max_upload_mb: int,
) -> APIRouter:
    router = APIRouter()
    routes = _KnowledgeRoutes(
        templates=templates, knowledge=knowledge, sports=sports, max_upload_mb=max_upload_mb
    )
    router.get("/admin/knowledge", response_class=HTMLResponse)(routes.page)
    router.post("/admin/knowledge/sources", response_class=HTMLResponse)(routes.upload)
    router.delete("/admin/knowledge/sources/{source_id}", response_class=HTMLResponse)(
        routes.delete
    )
    router.post("/admin/knowledge/sources/{source_id}/reingest", response_class=HTMLResponse)(
        routes.reingest
    )
    return router
