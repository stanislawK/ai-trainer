"""The AI onboarding's routes (PRD 0008 F6, F20, F21, ADR-0028, tickets #254-#256): the sport
card, the About you chat card, each sport's goals, level and schedule chat cards under
`/onboarding/<sport_id>/`, all with Skip and Back, "Add another sport?", the summary that
catches up on reads over SSE, and its confirm. No request here awaits a model call except the
summary stream (invariant 2), and `user_id` comes only from the authenticated session
(ADR-0005)."""

from collections.abc import AsyncIterable, Mapping

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.datastructures import FormData
from starlette.templating import Jinja2Templates

from ai_trainer.application.ai_onboarding import (
    SPORT_STEPS,
    AlreadyPickedError,
    EmptyAnswerError,
    InvalidSummaryError,
    OnboardingCard,
    OnboardingSummary,
    Place,
    add_sport,
    answer_of,
    catch_up,
    confirm_summary,
    current_card,
    flow_of,
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
from ai_trainer.domain.sport_profile import (
    MAX_EXERCISES_LENGTH,
    MAX_GOAL_LENGTH,
    MAX_LEVEL_LENGTH,
    SportForm,
    SportSection,
)
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.users import User

_UNPROCESSABLE = 422
_HOME_PATH = "/"
_CARD_PATHS = {
    OnboardingCard.SPORT: "/onboarding/sport",
    OnboardingCard.ABOUT_YOU: "/onboarding/about-you",
    OnboardingCard.MORE: "/onboarding/more",
    OnboardingCard.SUMMARY: "/onboarding/summary",
}
# The fields the summary posts, as typed; a sport's are named `<sport_id>.<field>`.
_SPORT, _ABOUT_YOU, _MORE, _SUMMARY = (
    Place(OnboardingCard.SPORT),
    Place(OnboardingCard.ABOUT_YOU),
    Place(OnboardingCard.MORE),
    Place(OnboardingCard.SUMMARY),
)
_SUMMARY_FIELDS = ("age", "height_cm", "weight_kg", "limits", "timezone")
_SPORT_FIELDS = ("level", "exercises", "sessions_per_week", "session_minutes")


def _path(place: Place) -> str:
    """A card's URL; a sport's step is `/onboarding/<sport_id>/<step>`."""
    if place.sport_id is None:
        return _CARD_PATHS[place.card]
    return f"/onboarding/{place.sport_id}/{place.card.value}"


def _progress(step: OnboardingStep, *, first: bool) -> tuple[int, int]:
    """A sport step's place in its round, counted from 0, and the round's length (Onboarding
    v4): the first sport's round is the sport, About you and its three steps; a later one's is
    "Add another sport?" and its three steps."""
    index = SPORT_STEPS.index(step)
    return (2 + index, 5) if first else (1 + index, 4)


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

    async def flow(user: User) -> tuple[Place, ...]:
        sports = await answers.list_sports(user.id)
        return flow_of(sports, sport_count=len(registry.all()))

    async def misplaced(request: Request, place: Place) -> Response | None:
        """Where to send an athlete who may not be on `place` yet, or at all: home once
        onboarded, else the card the server says is due when that is an earlier one or `place`
        isn't in their flow (a sport not picked, or "Add another sport?" with none left)."""
        user: User = request.state.user
        if user.onboarded_at is not None:
            return go(request, _HOME_PATH)
        due = await current_card(user.id, answers=answers, registry=registry)
        places = await flow(user)
        if place not in places:
            return go(request, _path(due))
        if due.card is OnboardingCard.MORE and place.card is OnboardingCard.SUMMARY:
            # "No, show my summary" stores nothing (#256): the summary opens while the offer is
            # due, and `/onboarding` comes back to the offer.
            return None
        if places.index(due) < places.index(place):
            return go(request, _path(due))
        return None

    async def beside(user: User, place: Place, offset: int) -> Place:
        """The card before (`-1`, Back) or after (`1`, Send and Skip) `place` in the flow."""
        places = await flow(user)
        return places[places.index(place) + offset]

    @router.get("")
    async def start(request: Request) -> Response:
        user: User = request.state.user
        if user.onboarded_at is not None:
            return go(request, _HOME_PATH)
        return go(request, _path(await current_card(user.id, answers=answers, registry=registry)))

    @router.get("/sport")
    async def sport_card(request: Request) -> Response:
        if (elsewhere := await misplaced(request, _SPORT)) is not None:
            return elsewhere
        user: User = request.state.user
        picked = await answers.list_sports(user.id)
        return render(
            request, "sport", {"sports": registry.all(), "picked": picked[0] if picked else None}
        )

    @router.post("/sport")
    async def choose_sport(request: Request) -> Response:
        if (elsewhere := await misplaced(request, _SPORT)) is not None:
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
        if (elsewhere := await misplaced(request, _ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        earlier = await answer_of(user.id, OnboardingStep.ABOUT_YOU, answers=answers)
        return render_about_you(request, earlier.text or "" if earlier is not None else "")

    @router.post("/about-you")
    async def answer_about_you(request: Request) -> Response:
        if (elsewhere := await misplaced(request, _ABOUT_YOU)) is not None:
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
        return go(request, _path(await beside(user, _ABOUT_YOU, 1)))

    @router.post("/about-you/skip")
    async def skip_about_you(request: Request) -> Response:
        if (elsewhere := await misplaced(request, _ABOUT_YOU)) is not None:
            return elsewhere
        user: User = request.state.user
        await skip_step(user.id, OnboardingStep.ABOUT_YOU, answers=answers)
        return go(request, _path(await beside(user, _ABOUT_YOU, 1)))

    def add_sport_step(step: OnboardingStep) -> None:
        """A sport's chat card (#255, #256): goals, level or schedule, asked about that sport,
        with Skip and Back, at `/onboarding/<sport_id>/<step>`."""
        card = OnboardingCard(step.value)
        path = f"/{{sport_id}}/{step.value}"

        async def render_card(
            request: Request,
            place: Place,
            text: str,
            *,
            error: bool = False,
            status_code: int = 200,
        ) -> HTMLResponse:
            user: User = request.state.user
            sport_id = place.sport_id or ""
            index, steps = _progress(
                step, first=sport_id == (await answers.list_sports(user.id))[0]
            )
            return render(
                request,
                "sport_step",
                {
                    "step": step.value,
                    "place": index,
                    "round_length": steps,
                    "back_href": _path(await beside(user, place, -1)),
                    "url": _path(place),
                    "sport": registry.get(sport_id),
                    "text": text,
                    "error": error,
                    "max_length": MAX_ANSWER_LENGTH,
                },
                status_code=status_code,
            )

        async def show(request: Request, sport_id: str) -> Response:
            place = Place(card, sport_id)
            if (elsewhere := await misplaced(request, place)) is not None:
                return elsewhere
            user: User = request.state.user
            earlier = await answer_of(user.id, step, answers=answers, sport_id=sport_id)
            return await render_card(request, place, earlier.text or "" if earlier else "")

        async def send(request: Request, sport_id: str) -> Response:
            place = Place(card, sport_id)
            if (elsewhere := await misplaced(request, place)) is not None:
                return elsewhere
            user: User = request.state.user
            text = (await request.form()).get("answer")
            typed = text if isinstance(text, str) else ""
            try:
                await send_answer(
                    user.id,
                    step,
                    typed,
                    answers=answers,
                    reads=reads,
                    reader=reader,
                    sport_id=sport_id,
                    timezone=user.timezone,
                )
            except EmptyAnswerError:
                return await render_card(
                    request, place, typed, error=True, status_code=_UNPROCESSABLE
                )
            return go(request, _path(await beside(user, place, 1)))

        async def skip(request: Request, sport_id: str) -> Response:
            place = Place(card, sport_id)
            if (elsewhere := await misplaced(request, place)) is not None:
                return elsewhere
            user: User = request.state.user
            await skip_step(user.id, step, answers=answers, sport_id=sport_id)
            return go(request, _path(await beside(user, place, 1)))

        router.add_api_route(path, show, methods=["GET"], name=f"onboarding_{step.value}_card")
        router.add_api_route(path, send, methods=["POST"], name=f"onboarding_{step.value}_send")
        router.add_api_route(
            f"{path}/skip", skip, methods=["POST"], name=f"onboarding_{step.value}_skip"
        )

    for step in SPORT_STEPS:
        add_sport_step(step)

    @router.get("/more")
    async def more_card(request: Request) -> Response:
        """ "Add another sport?" (#256): the sports not picked yet, and the way to the summary."""
        if (elsewhere := await misplaced(request, _MORE)) is not None:
            return elsewhere
        user: User = request.state.user
        picked = await answers.list_sports(user.id)
        return render(
            request,
            "more",
            {
                "sports": [sport for sport in registry.all() if sport.id not in picked],
                "back_href": _path(await beside(user, _MORE, -1)),
            },
        )

    @router.post("/more")
    async def choose_another_sport(request: Request) -> Response:
        if (elsewhere := await misplaced(request, _MORE)) is not None:
            return elsewhere
        user: User = request.state.user
        sport = (await request.form()).get("sport")
        sport_id = sport if isinstance(sport, str) else ""
        try:
            await add_sport(user.id, sport_id, registry=registry, answers=answers)
        except (UnknownSportError, AlreadyPickedError) as exc:
            raise HTTPException(status_code=_UNPROCESSABLE, detail=str(exc)) from exc
        return go(request, _path(Place(OnboardingCard.GOALS, sport_id)))

    @router.get("/summary")
    async def summary_page(request: Request) -> Response:
        if (elsewhere := await misplaced(request, _SUMMARY)) is not None:
            return elsewhere
        user: User = request.state.user
        return render(
            request,
            "summary",
            {"sports": [registry.get(sport) for sport in await answers.list_sports(user.id)]},
        )

    def summary_context(
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
            plugin = registry.get(sport.sport_id)
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
        if (elsewhere := await misplaced(request, _SUMMARY)) is not None:
            return elsewhere
        user: User = request.state.user
        form = await request.form()
        typed = {
            field: value if isinstance(value := form.get(field, ""), str) else ""
            for field in _SUMMARY_FIELDS
        }
        sport_forms = {
            sport_id: _typed_form(form, sport_id) for sport_id in await answers.list_sports(user.id)
        }
        try:
            await confirm_summary(
                user.id, typed, sport_forms=sport_forms, answers=answers, clock=clock
            )
        except InvalidSummaryError as exc:
            summary = await summary_of(user.id, answers=answers, timezone=user.timezone)
            return render(
                request,
                "summary_form",
                summary_context(
                    summary, values=typed, sport_forms=sport_forms, problems=exc.problems
                ),
                status_code=_UNPROCESSABLE,
            )
        return go(request, _HOME_PATH)

    return router
