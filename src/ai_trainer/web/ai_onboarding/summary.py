"""The summary (F20, F21, B35): its page, the stream that catches up on every unread answer and
then sends the editable summary, and its confirm, the onboarding's only write to the profile."""

from collections.abc import AsyncIterable, Mapping

from fastapi import APIRouter, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.datastructures import FormData

from ai_trainer.application.ai_onboarding import (
    InvalidSummaryError,
    OnboardingSummary,
    catch_up,
    confirm_summary,
    summary_of,
)
from ai_trainer.application.onboarding import timezone_names
from ai_trainer.domain.athlete_profile import MAX_LIMITS_LENGTH, FieldProblem
from ai_trainer.domain.sport_profile import (
    MAX_EXERCISES_LENGTH,
    MAX_GOAL_LENGTH,
    MAX_LEVEL_LENGTH,
    SportForm,
    SportSection,
)
from ai_trainer.domain.users import User
from ai_trainer.web.ai_onboarding.context import (
    HOME_PATH,
    SUMMARY,
    UNPROCESSABLE,
    OnboardingContext,
    go,
)

# The fields the summary posts, as typed; a sport's are named `<sport_id>.<field>`.
_SUMMARY_FIELDS = ("age", "height_cm", "weight_kg", "limits", "timezone")
_SPORT_FIELDS = ("level", "exercises", "sessions_per_week", "session_minutes")


def _shown_form(section: SportSection) -> SportForm:
    """A read section as the summary's fields show it, with one goal row even when none was
    read, so the athlete can add one."""
    profile = section.profile
    goals = section.goals or ()
    return SportForm(
        fields={
            "level": profile.level or "",
            "exercises": profile.exercises or "",
            "sessions_per_week": str(profile.sessions_per_week or ""),
            "session_minutes": str(profile.session_minutes or ""),
        },
        days=tuple(str(day) for day in profile.preferred_days),
        goal_texts=tuple(goal.text for goal in goals) or ("",),
        goal_dates=tuple(goal.target_date.isoformat() if goal.target_date else "" for goal in goals)
        or ("",),
    )


def _typed_form(form: FormData, sport_id: str) -> SportForm:
    def all_of(name: str) -> tuple[str, ...]:
        return tuple(
            value for value in form.getlist(f"{sport_id}.{name}") if isinstance(value, str)
        )

    return SportForm(
        fields={
            field: value if isinstance(value := form.get(f"{sport_id}.{field}", ""), str) else ""
            for field in _SPORT_FIELDS
        },
        days=all_of("days"),
        goal_texts=all_of("goal_text") or ("",),
        goal_dates=all_of("goal_date") or ("",),
    )


class _SummaryRoutes:
    def __init__(self, ctx: OnboardingContext) -> None:
        self._ctx = ctx

    def _context(
        self,
        summary: OnboardingSummary,
        *,
        values: Mapping[str, str] | None = None,
        sport_forms: Mapping[str, SportForm] | None = None,
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
        sports = []
        for sport in summary.sports:
            plugin = self._ctx.registry.get(sport.sport_id)
            sports.append(
                {
                    "id": sport.sport_id,
                    "icon": plugin.icon,
                    "label_key": plugin.label_key,
                    "skipped": sport.skipped,
                    "form": (sport_forms or {}).get(sport.sport_id) or _shown_form(sport.section),
                }
            )
        return {
            "values": shown,
            "sports": sports,
            "problems": problems or {},
            "skipped": summary.about_you_skipped,
            "zones": timezone_names(),
            "max_limits": MAX_LIMITS_LENGTH,
            "max_goal": MAX_GOAL_LENGTH,
            "max_level": MAX_LEVEL_LENGTH,
            "max_exercises": MAX_EXERCISES_LENGTH,
        }

    async def summary_page(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, SUMMARY)) is not None:
            return elsewhere
        user: User = request.state.user
        return ctx.render(
            request,
            "summary",
            {
                "sports": [
                    ctx.registry.get(sport) for sport in await ctx.answers.list_sports(user.id)
                ]
            },
        )

    async def stream_summary(self, request: Request) -> AsyncIterable[ServerSentEvent]:
        """Finishes every unread answer, then sends the editable summary in place of the
        skeleton (B35). The only onboarding request that waits on a model call."""
        ctx = self._ctx
        user: User = request.state.user
        await catch_up(
            user.id, answers=ctx.answers, reads=ctx.reads, reader=ctx.reader, timezone=user.timezone
        )
        summary = await summary_of(user.id, answers=ctx.answers, timezone=user.timezone)
        html = ctx.templates.get_template("partials/onboarding/summary_ready.html").render(
            request=request,
            csrf_token=getattr(request.state, "csrf_token", None),
            **self._context(summary),
        )
        yield ServerSentEvent(raw_data=html)

    async def confirm(self, request: Request) -> Response:
        ctx = self._ctx
        if (elsewhere := await ctx.misplaced(request, SUMMARY)) is not None:
            return elsewhere
        user: User = request.state.user
        form = await request.form()
        typed = {
            field: value if isinstance(value := form.get(field, ""), str) else ""
            for field in _SUMMARY_FIELDS
        }
        sport_forms = {
            sport_id: _typed_form(form, sport_id)
            for sport_id in await ctx.answers.list_sports(user.id)
        }
        try:
            await confirm_summary(
                user.id, typed, sport_forms=sport_forms, answers=ctx.answers, clock=ctx.clock
            )
        except InvalidSummaryError as exc:
            summary = await summary_of(user.id, answers=ctx.answers, timezone=user.timezone)
            return ctx.render(
                request,
                "summary_form",
                self._context(
                    summary, values=typed, sport_forms=sport_forms, problems=exc.problems
                ),
                status_code=UNPROCESSABLE,
            )
        return go(request, HOME_PATH)


def build_summary_router(ctx: OnboardingContext) -> APIRouter:
    router = APIRouter(prefix="/onboarding")
    routes = _SummaryRoutes(ctx)
    router.get("/summary")(routes.summary_page)
    router.get("/summary/stream", response_class=EventSourceResponse)(routes.stream_summary)
    router.post("/confirm")(routes.confirm)
    return router
