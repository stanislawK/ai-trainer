"""`GET /history/{session_id}/exercises/{index}` (PRD 0011 M2g, B37, B40, ticket #517): one
exercise's progress over time, opened from an exercise row of a saved session. `index` is the
entry's 0-based place in that session. A session that isn't the athlete's, an index past its
entries and an exercise they can't see are all a 404 (ADR-0021)."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.exercise_progress import ExerciseProgress, ProgressView
from ai_trainer.domain.exercises import ExerciseMeasure
from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import NOT_FOUND, parse_id

_UNITS = {
    ExerciseMeasure.LOAD_REPS: "kg",
    ExerciseMeasure.REPS: "reps",
    ExerciseMeasure.TIME: "s",
    ExerciseMeasure.DISTANCE: "m",
    ExerciseMeasure.BAND_REPS: "",
}


def _chart(view: ProgressView) -> dict[str, object]:
    """What `static/js/charts.js` reads from the canvas: a label and a value per day, oldest
    first, `null` for a band with no place, and for a band chart its order for the axis."""
    pattern = "%-d %b %Y" if view.spans_years else "%-d %b"
    return {
        "labels": [point.day.strftime(pattern) for point in view.points],
        "values": [point.value for point in view.points],
        "unit": _UNITS[view.measure],
        "order": list(view.order),
    }


def build_exercise_progress_router(
    templates: Jinja2Templates, *, progress: ExerciseProgress
) -> APIRouter:
    router = APIRouter()

    @router.get("/history/{session_id}/exercises/{index}", response_class=HTMLResponse)
    async def exercise_progress(request: Request, session_id: str, index: int) -> HTMLResponse:
        user: User = request.state.user
        view = await progress.view(user.id, user.timezone, parse_id(session_id), index)
        if view is None:
            raise HTTPException(status_code=NOT_FOUND)
        return templates.TemplateResponse(
            request,
            "pages/history/progress.html",
            {"view": view, "chart": _chart(view)},
        )

    return router
