"""The picture the exercise card draws for a candidate with none (ADR-0031 *Picture on the card*,
#643; #641 option B): a drawable catalogue candidate shows a skeleton that posts on load; the
route draws the picture under the athlete whose card asked and returns it with its credit out of
band; another athlete's card then shows it at once with no call; any fallback answers with nothing,
which leaves the text-only row; a custom exercise or one with a wger picture gets no slot. The
brief writer, Jev's check and the image model are counting fakes: no model is called."""

import re
from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.application.exercise_card_media import ExerciseCardMedia
from ai_trainer.application.exercise_card_picture import (
    CardPictureLimits,
    CardPicturePorts,
    CardPictures,
    DrawCardPicture,
)
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel, ExerciseText
from ai_trainer.domain.media import MediaAsset, MediaDelivery, MediaKind, MediaRights
from ai_trainer.domain.media_generation import ImageGenerationFailed, PictureSubject
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.web.templating import build_templates
from tests.card_picture_support import (
    FakeBriefs,
    FakeCardMedia,
    FakeCheck,
    FakeImages,
    FakeSpend,
)
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryClarificationRepository,
    InMemoryProfile,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)
from tests.picker_support import ScriptedPicker
from tests.picture_support import FrozenClock, InMemoryBucket

_HTMX = {"HX-Request": "true"}
_BASE = "https://media.example.test"
_MODEL = "google/gemini-3.1-flash-lite-image-20260630"
_PULL = ExerciseCandidate(id=uuid4(), name="Pull-Up", how="Hang from the bar, then pull up.")
_GOBLET = ExerciseCandidate(id=uuid4(), name="Goblet Squat", how="Hold a dumbbell at your chest.")
# An athlete's own exercise: no catalogue text, so never drawable.
_CUSTOM = ExerciseCandidate(id=uuid4(), name="My Pull", how="My way.")


def _subject(candidate: ExerciseCandidate) -> PictureSubject:
    return PictureSubject(
        exercise_id=candidate.id,
        name=candidate.name,
        sport_id="gym",
        text=ExerciseText(lang="en", instructions=candidate.how or "", steps=()),
    )


def _wger() -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key="wger/goblet.png",
        source="wger",
        source_ref="https://wger.de/api/v2/exerciseimage/7/",
        license="CC BY-SA 4.0",
        attribution="An author · CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)",
        sha256="a" * 64,
    )


class _Names:
    def __init__(self, candidates: Sequence[ExerciseCandidate]) -> None:
        self.candidates = candidates

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> dict[str, UUID]:
        return {}

    async def labels(
        self, user_id: UUID, *, named: object, near: float
    ) -> dict[str, ExerciseLabel]:
        return {}

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        return self.candidates


def _extraction() -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [{"name": "pulls", "sets": 3, "reps": 8}],
        }
    )


def _media() -> FakeCardMedia:
    """The shared catalogue: two catalogue candidates with English text, one of them with an
    approved wger picture, and an athlete's own exercise with neither."""
    return FakeCardMedia(
        subjects={candidate.id: _subject(candidate) for candidate in (_PULL, _GOBLET)},
        approved={_GOBLET.id: _wger()},
    )


class _App:
    def __init__(
        self,
        media: FakeCardMedia | None = None,
        *,
        clarifications: InMemoryClarificationRepository | None = None,
        pictures: bool = True,
    ) -> None:
        app = FastAPI()
        self.media = media or _media()
        self.briefs = FakeBriefs()
        self.check = FakeCheck()
        self.images = FakeImages()
        self.bucket = InMemoryBucket()
        self.clarifications = clarifications or InMemoryClarificationRepository()
        self.profile = InMemoryProfile()
        card_media = ExerciseCardMedia(media=self.media, public_base_url=_BASE)
        draw = DrawCardPicture(
            CardPicturePorts(
                media=self.media,
                spend=FakeSpend(),
                briefs=self.briefs,
                brief_check=self.check,
                images=self.images,
                storage=self.bucket,
            ),
            limits=CardPictureLimits(
                enabled=True, daily_cap_usd=Decimal("1.00"), price_usd=Decimal("0.0336")
            ),
            clock=FrozenClock(),
            model=_MODEL,
        )
        conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]], extractions=[_extraction()]
        )
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=conversation,
            profile=self.profile,
            clarifications=self.clarifications,
            settlers={
                "gym": ResolveExercises(
                    _Names((_PULL, _GOBLET, _CUSTOM)),
                    ScriptedPicker(),
                    question=conversation.exercise_question,
                    limits=ResolutionLimits(
                        near_match=0.75, candidate_floor=0.25, shortlist_size=4
                    ),
                )
            },
            card_media=card_media,
            card_pictures=(
                CardPictures(media=self.media, draw=draw, card_media=card_media)
                if pictures
                else None
            ),
        )
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = ["gym"]
        self.client = TestClient(app)

    @property
    def model_calls(self) -> int:
        return len(self.briefs.calls) + len(self.check.calls) + len(self.images.calls)

    async def reply(self) -> str:
        message = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            "pulls 3x8",
            FROZEN_NOW,
        )
        return " ".join(self.client.get(f"/messages/{message.id}/reply").text.split())

    def open_question(self) -> UUID:
        [question] = [
            row.id for row in self.clarifications.all_of(self.user.id) if row.status == "open"
        ]
        return question

    def post(self, question: UUID, option: str) -> tuple[int, str]:
        response = self.client.post(f"/clarifications/{question}/picture/{option}", headers=_HTMX)
        return response.status_code, " ".join(response.text.split())


def _candidate(html: str, candidate: ExerciseCandidate) -> str:
    """The candidate's `group/cand` wrapper, up to the next candidate or "Keep my name"."""
    start = html.rindex("<", 0, html.index(f'data-candidate="{candidate.id}"'))
    following = html.find("data-choice-option=", html.index(f'data-choice-option="{candidate.id}"'))
    following = html.find("data-choice-option=", following + 1)
    end = html.rindex("<", 0, following) if following != -1 else len(html)
    return html[start:end]


def _slot_url(question: UUID, candidate: ExerciseCandidate) -> str:
    return f'hx-post="/clarifications/{question}/picture/{candidate.id}"'


# AC 1: a drawable candidate shows the loading slot, which posts as soon as it shows.
async def test_card_shows_picture_skeleton_for_drawable_candidate() -> None:
    app = _App()

    html = await app.reply()

    question = app.open_question()
    block = _candidate(html, _PULL)
    [slot] = re.findall(r"<span[^>]*data-picture-load[^>]*>", block)
    assert _slot_url(question, _PULL) in slot
    assert 'hx-trigger="load"' in slot
    assert 'hx-target="this"' in slot
    assert 'hx-swap="outerHTML"' in slot
    assert "skeleton" in slot
    assert '<span class="sr-only" role="status" aria-busy="true">Drawing a picture</span>' in block
    # The credit's empty landing place, which the route fills out of band.
    assert f'<div id="choice-{question}-credit-{_PULL.id}" hidden data-credit-slot></div>' in block
    assert "<img" not in block
    assert app.model_calls == 0


# AC 1: the slot's request draws the picture and returns it with its credit.
async def test_picture_route_draws_and_returns_thumbnail_with_credit() -> None:
    app = _App()
    await app.reply()
    question = app.open_question()

    status, body = app.post(question, str(_PULL.id))

    assert status == 200
    [(asset, exercise_id)] = app.media.adds
    assert exercise_id == _PULL.id
    assert body.startswith("<span")
    assert "data-picture" in body
    assert "bg-primary/10" in body
    assert f'<img src="{_BASE}/{asset.storage_key}"' in body
    assert "data-picture-load" not in body
    credit = body[body.index(f'id="choice-{question}-credit-{_PULL.id}"') :]
    assert 'class="contents"' in credit
    assert 'hx-swap-oob="true"' in credit
    assert "AI generated with" in credit
    assert [user for user, _ in app.briefs.calls] == [app.user.id]
    assert [user for user, _ in app.images.calls] == [app.user.id]
    # The question stays open: the slot never answers it.
    assert app.open_question() == question


# AC 2: once drawn, another athlete's card shows the picture at once with no model call.
async def test_second_athlete_sees_drawn_picture_without_model_call() -> None:
    first = _App()
    await first.reply()
    first.post(first.open_question(), str(_PULL.id))
    [(asset, _)] = first.media.adds
    second = _App(first.media)

    html = await second.reply()

    block = _candidate(html, _PULL)
    assert f'<img src="{_BASE}/{asset.storage_key}"' in block
    assert "data-picture-load" not in block
    assert "skeleton" not in block
    assert "AI generated with" in block
    assert second.model_calls == 0


# AC 3: a fallback answers with nothing, so the slot goes and the row is text only.
async def test_picture_route_fallback_returns_text_only_row() -> None:
    app = _App()
    app.images.reply = ImageGenerationFailed(reason="timeout")
    await app.reply()

    status, body = app.post(app.open_question(), str(_PULL.id))

    assert status == 200
    assert body == ""
    assert app.media.adds == []
    assert app.bucket.objects == {}


# AC 5: a custom exercise or one with a wger picture gets no slot and no call.
async def test_card_has_no_skeleton_for_custom_or_wger_candidate() -> None:
    app = _App()

    html = await app.reply()

    question = app.open_question()
    goblet = _candidate(html, _GOBLET)
    assert _slot_url(question, _GOBLET) not in goblet
    assert f'src="{_BASE}/wger/goblet.png"' in goblet
    custom = _candidate(html, _CUSTOM)
    assert "data-picture-load" not in custom
    assert "data-credit-slot" not in custom
    assert app.media.drawable_reads == [{_PULL.id, _CUSTOM.id}]
    assert app.model_calls == 0


async def test_another_users_question_is_404() -> None:
    app = _App()
    await app.reply()
    stranger = _App(app.media, clarifications=app.clarifications)

    status, _ = stranger.post(app.open_question(), str(_PULL.id))

    assert status == 404
    assert stranger.model_calls == 0


async def test_unknown_question_or_option_is_404() -> None:
    app = _App()
    await app.reply()
    question = app.open_question()

    for option in (str(uuid4()), "keep", "not-an-id"):
        assert app.post(question, option)[0] == 404
    assert app.post(uuid4(), str(_PULL.id))[0] == 404
    assert app.model_calls == 0


async def test_answered_question_is_409() -> None:
    app = _App()
    await app.reply()
    question = app.open_question()
    app.client.post(
        f"/clarifications/{question}/answer",
        data={"option_ids": str(_PULL.id)},
        headers=_HTMX,
    )

    assert app.post(question, str(_PULL.id))[0] == 409
    assert app.model_calls == 0


async def test_without_the_picture_service_there_is_no_slot_and_the_route_is_404() -> None:
    app = _App(pictures=False)

    html = await app.reply()

    assert "data-picture-load" not in html
    assert "data-credit-slot" not in html
    assert app.post(app.open_question(), str(_PULL.id))[0] == 404


async def test_the_how_to_route_is_still_404_without_its_service() -> None:
    """The picture and how-to routes share their guards; each service's absence is its own
    404."""
    app = _App()
    await app.reply()

    response = app.client.post(
        f"/clarifications/{app.open_question()}/howto/{_PULL.id}", headers=_HTMX
    )

    assert response.status_code == 404
