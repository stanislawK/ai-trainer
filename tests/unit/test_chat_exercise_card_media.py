"""Approved pictures on the exercise card (F23, F24, ADR-0031, #558; the Exercise Card mock's
media frames and #551's credit line): a candidate with an approved picture shows it, 48 px on the
phone and 64 px wider, with its full credit just below its row; one without shows no picture and
no empty slot; with no public media base the card is text only. Every open card's candidates are
read in one call."""

import re
from collections.abc import Collection, Mapping, Sequence
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.application.exercise_card_media import ExerciseCardMedia
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.media import MediaAsset, MediaDelivery, MediaKind, MediaRights
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)
from tests.picker_support import ScriptedPicker

_HTMX = {"HX-Request": "true"}
_BASE = "https://media.example.test"
_LICENCE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
_PAGE = "https://wger.de/api/v2/exerciseimage/7/"
_GOBLET = ExerciseCandidate(id=uuid4(), name="Goblet Squat", how="Hold a dumbbell at your chest.")
_FRONT = ExerciseCandidate(id=uuid4(), name="Front Squat", how="Bar on the front of the shoulders.")
_BOX = ExerciseCandidate(id=uuid4(), name="Box Squat", how="Sit back onto a box, then stand.")


def _wger(*, ai: bool = False) -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key="wger/goblet squat.png",
        source="wger",
        source_ref=_PAGE + ("#ai-generated" if ai else ""),
        license="CC BY-SA 4.0",
        attribution=("A long author name, Jr. · CC BY-SA 4.0 (" + _LICENCE_URL + ") — via wger.de"),
        sha256="a" * 64,
    )


def _generated() -> MediaAsset:
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key="generated/front.png",
        source="ai-generated",
        source_ref="google/gemini-3.1-flash-lite-image",
        license="generated",
        attribution=(
            "AI generated with Gemini 3.1 Flash Lite Image (Google) via OpenRouter · 2026-11-08"
        ),
        sha256="b" * 64,
    )


class _Media:
    """`ExerciseCardMediaPort` over a fixed map, counting its reads."""

    def __init__(self, pictures: Mapping[UUID, MediaAsset]) -> None:
        self.pictures = dict(pictures)
        self.calls: list[set[UUID]] = []

    async def first_approved_pictures(
        self, exercise_ids: Collection[UUID]
    ) -> Mapping[UUID, MediaAsset]:
        self.calls.append(set(exercise_ids))
        return {key: value for key, value in self.pictures.items() if key in exercise_ids}


class _Names:
    def __init__(self, candidates: Sequence[ExerciseCandidate]) -> None:
        self.candidates = candidates

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> dict[str, UUID]:
        return {}

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> dict[str, ExerciseLabel]:
        return {typed: ExerciseLabel(name=_GOBLET.name) for typed in named}

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


def _extraction(*names: str) -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [{"name": name, "sets": 3, "reps": 10} for name in names],
        }
    )


class _App:
    def __init__(
        self,
        pictures: Mapping[UUID, MediaAsset],
        *,
        logs: Sequence[Sequence[str]] = (("squat",),),
        base_url: str | None = _BASE,
    ) -> None:
        app = FastAPI()
        self.media = _Media(pictures)
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.profile = InMemoryProfile()
        conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")] for _ in logs],
            extractions=[_extraction(*names) for names in logs],
        )
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=conversation,
            profile=self.profile,
            drafts=self.drafts,
            clarifications=self.clarifications,
            settlers={
                "gym": ResolveExercises(
                    _Names((_GOBLET, _FRONT, _BOX)),
                    ScriptedPicker(),
                    question=conversation.exercise_question,
                    limits=ResolutionLimits(
                        near_match=0.75, candidate_floor=0.25, shortlist_size=4
                    ),
                )
            },
            card_media=ExerciseCardMedia(media=self.media, public_base_url=base_url),
        )
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = ["gym"]
        self.client = TestClient(app)

    async def reply(self, text: str = "squat 3x10") -> str:
        message = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            text,
            FROZEN_NOW,
        )
        return " ".join(self.client.get(f"/messages/{message.id}/reply").text.split())

    def open_question(self) -> UUID:
        [question] = [
            row.id for row in self.clarifications.all_of(self.user.id) if row.status == "open"
        ]
        return question


def _candidate(html: str, candidate: ExerciseCandidate) -> str:
    """The candidate's block, up to the next option's row: its row button and, with a picture,
    the credit below it."""
    wrapped = f'data-candidate="{candidate.id}"'
    marker = wrapped if wrapped in html else f'data-choice-option="{candidate.id}"'
    start = html.rindex("<", 0, html.index(marker))
    following = html.find(
        "data-choice-option=", html.index(f'data-choice-option="{candidate.id}"') + 1
    )
    end = html.rindex("<", 0, following) if following != -1 else len(html)
    return html[start:end]


async def test_candidate_with_approved_wger_picture_shows_picture_and_full_credit() -> None:
    app = _App({_GOBLET.id: _wger()})

    html = await app.reply()

    block = _candidate(html, _GOBLET)
    [img] = re.findall(r"<img[^>]*>", block)
    assert f'src="{_BASE}/wger/goblet%20squat.png"' in img
    assert 'alt=""' in img
    assert "object-contain" in img
    # 48 px on the phone, the mock's 64 px wider, on the neutral fill.
    assert "size-12 sm:size-16" in block
    assert "bg-base-content/10" in block
    # The how-to clamps at two lines beside the picture on the phone and wider (#580).
    assert "line-clamp-2" in block
    assert "line-clamp-1" not in block
    # The credit wraps rather than truncates, so the licence always shows; it is one quiet line
    # of dot-separated text links (#580).
    assert re.search(r'<span class="[^"]*\bflex-wrap\b[^"]*" data-credit="wger"', block)
    credit = block[block.index('data-credit="wger"') :]
    assert "<span>A long author name, Jr.</span>" in credit
    assert f'href="{_LICENCE_URL}"' in credit
    assert ">CC BY-SA 4.0</a>" in credit
    assert '<span aria-hidden="true">·</span>' in credit
    assert f'href="{_PAGE}"' in credit
    assert "wger.de" in credit
    assert "truncate" not in block
    assert 'data-mark="ai"' not in block


async def test_credit_sits_below_the_row_button_not_inside_it() -> None:
    app = _App({_GOBLET.id: _wger()})

    html = await app.reply()

    block = _candidate(html, _GOBLET)
    assert block.index("</button>") < block.index('data-credit="wger"')


async def test_candidate_without_approved_picture_has_no_picture_slot() -> None:
    app = _App({_GOBLET.id: _wger()})

    html = await app.reply()

    block = _candidate(html, _FRONT)
    assert "<img" not in block
    assert "size-12" not in block
    assert "data-credit" not in block
    assert "line-clamp-2" in block


async def test_no_approved_picture_at_all_leaves_the_card_text_only() -> None:
    app = _App({})

    html = await app.reply()

    assert "<img" not in html
    assert "data-credit" not in html


async def test_no_public_base_url_leaves_the_card_text_only() -> None:
    app = _App({_GOBLET.id: _wger()}, base_url=None)

    html = await app.reply()

    assert "Goblet Squat" in html
    assert "<img" not in html
    assert "data-credit" not in html
    assert app.media.calls == []


async def test_generated_picture_credit_says_ai_generated() -> None:
    app = _App({_FRONT.id: _generated()})

    html = await app.reply()

    block = _candidate(html, _FRONT)
    assert f'src="{_BASE}/generated/front.png"' in block
    assert (
        "AI generated with Gemini 3.1 Flash Lite Image (Google) via OpenRouter · 2026-11-08"
        in block
    )
    # Its credit already says so: no extra mark (#551).
    assert 'data-mark="ai"' not in block
    # The mock draws a generated picture on the primary tint, as the admin page does.
    assert "bg-primary/10" in block


async def test_ai_flagged_wger_picture_shows_ai_generated_badge() -> None:
    app = _App({_GOBLET.id: _wger(ai=True)})

    html = await app.reply()

    block = _candidate(html, _GOBLET)
    assert '<span class="badge badge-soft badge-sm" data-mark="ai">AI generated</span>' in block
    assert f'href="{_PAGE}"' in block


async def test_the_card_reads_media_once_for_all_its_candidates() -> None:
    app = _App({_GOBLET.id: _wger()})

    await app.reply()

    assert app.media.calls == [{_GOBLET.id, _FRONT.id, _BOX.id}]


async def test_the_reloaded_thread_reads_media_once_and_shows_the_picture() -> None:
    app = _App({_GOBLET.id: _wger()})
    await app.reply()
    app.media.calls.clear()

    html = app.client.get("/").text

    assert app.media.calls == [{_GOBLET.id, _FRONT.id, _BOX.id}]
    assert html.count(f'src="{_BASE}/wger/goblet%20squat.png"') == 1


async def test_the_name_fields_back_shows_the_pictures_again() -> None:
    app = _App({_GOBLET.id: _wger()})
    await app.reply()

    html = app.client.get(f"/clarifications/{app.open_question()}/card", headers=_HTMX).text

    assert f'src="{_BASE}/wger/goblet%20squat.png"' in html


async def test_the_next_names_card_after_an_answer_shows_its_pictures() -> None:
    app = _App({_GOBLET.id: _wger()}, logs=(("squat", "deep squat"),))
    await app.reply()

    html = app.client.post(
        f"/clarifications/{app.open_question()}/answer",
        data={"option_ids": str(_FRONT.id)},
        headers=_HTMX,
    ).text

    assert "Which exercise is “deep squat”?" in html
    assert f'src="{_BASE}/wger/goblet%20squat.png"' in html


async def test_an_answered_question_reads_no_media_and_shows_no_picture() -> None:
    app = _App({_GOBLET.id: _wger()})
    await app.reply()
    app.client.post(
        f"/clarifications/{app.open_question()}/answer",
        data={"option_ids": str(_GOBLET.id)},
        headers=_HTMX,
    )
    app.media.calls.clear()

    html = app.client.get("/").text

    assert app.media.calls == []
    assert "<img" not in html
