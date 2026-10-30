"""What every onboarding sub-router shares: its services, each card's URL, and the helpers that
render a card, redirect, and keep an athlete on the card that is due (ticket #331)."""

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.ai_onboarding import OnboardingCard, Place, current_card, flow_of
from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingAnswersPort, OnboardingReaderPort
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.users import User

UNPROCESSABLE = 422
HOME_PATH = "/"
CARD_PATHS = {
    OnboardingCard.SPORT: "/onboarding/sport",
    OnboardingCard.ABOUT_YOU: "/onboarding/about-you",
    OnboardingCard.MORE: "/onboarding/more",
    OnboardingCard.SUMMARY: "/onboarding/summary",
}
SPORT, ABOUT_YOU, MORE, SUMMARY = (
    Place(OnboardingCard.SPORT),
    Place(OnboardingCard.ABOUT_YOU),
    Place(OnboardingCard.MORE),
    Place(OnboardingCard.SUMMARY),
)


def path(place: Place) -> str:
    """A card's URL; a sport's step is `/onboarding/<sport_id>/<step>`."""
    if place.sport_id is None:
        return CARD_PATHS[place.card]
    return f"/onboarding/{place.sport_id}/{place.card.value}"


def go(request: Request, to: str) -> Response:
    # htmx never processes redirect headers on a 3xx, so an htmx request gets
    # `HX-Redirect` on a 2xx instead.
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": to})
    return RedirectResponse(url=to, status_code=303)


@dataclass(frozen=True, slots=True)
class OnboardingContext:
    templates: Jinja2Templates
    registry: SportRegistry
    answers: OnboardingAnswersPort
    reads: BackgroundReads
    reader: OnboardingReaderPort
    clock: ClockPort

    def render(
        self, request: Request, page: str, context: Mapping[str, object], *, status_code: int = 200
    ) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        prefix = "partials" if is_htmx else "pages"
        return self.templates.TemplateResponse(
            request, f"{prefix}/onboarding/{page}.html", dict(context), status_code=status_code
        )

    async def flow(self, user: User) -> tuple[Place, ...]:
        sports = await self.answers.list_sports(user.id)
        return flow_of(sports, sport_count=len(self.registry.all()))

    async def due(self, user: User) -> Place:
        return await current_card(user.id, answers=self.answers, registry=self.registry)

    async def misplaced(self, request: Request, place: Place) -> Response | None:
        """Where to send an athlete who may not be on `place` yet, or at all: home once
        onboarded, else the card the server says is due when that is an earlier one or `place`
        isn't in their flow (a sport not picked, or "Add another sport?" with none left)."""
        user: User = request.state.user
        if user.onboarded_at is not None:
            return go(request, HOME_PATH)
        due = await self.due(user)
        places = await self.flow(user)
        if place not in places:
            return go(request, path(due))
        if due.card is OnboardingCard.MORE and place.card is OnboardingCard.SUMMARY:
            # "No, show my summary" stores nothing (#256): the summary opens while the offer is
            # due, and `/onboarding` comes back to the offer.
            return None
        if places.index(due) < places.index(place):
            return go(request, path(due))
        return None

    async def beside(self, user: User, place: Place, offset: int) -> Place:
        """The card before (`-1`, Back) or after (`1`, Send and Skip) `place` in the flow."""
        places = await self.flow(user)
        return places[places.index(place) + offset]
