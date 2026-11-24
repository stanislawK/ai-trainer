""" "Looks wrong" on a picture drawn on the exercise card (ADR-0031 *Picture on the card*, *Flag*,
#644; #641 option B, "picture: arrived" and "picture: just flagged"): the flag icon button sits on
the drawn picture's credit line, outside the row button; a tap takes the link down through the
takedown path, so every athlete's next card shows no picture and draws none, records one
content-free `picture_flagged` event and answers the candidate with the thanks line. The question
stays open. The admin sees the link under Removed and Restore shows it again. A wger picture and an
admin-approved generated one have no flag, and the route leaves them as they are. The catalogue
media, buckets and CDN are in memory; no model is called."""

import re
from collections.abc import Collection, Sequence
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
from ai_trainer.application.exercise_card_picture_flag import CardPictureFlag
from ai_trainer.application.exercise_media_review import Restored
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.media_quarantine import MediaQuarantine, MediaTakedown
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel, ExerciseText
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.media import MediaAsset, MediaLinkStatus
from ai_trainer.domain.media_generation import GENERATED_PICTURE_SOURCE, PictureSubject
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.web.templating import build_templates
from tests.card_picture_support import FakeBriefs, FakeCheck, FakeImages, FakeSpend
from tests.chat_support import (
    FROZEN_NOW,
    FixedClock,
    InMemoryClarificationRepository,
    InMemoryProfile,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)
from tests.exercise_media_support import (
    FakeCdn,
    MemoryExerciseMedia,
    ReviewClock,
    card_drawn_asset,
    generated_asset,
    review_with_takedown,
    wger_asset,
)
from tests.feedback_support import InMemoryFeedbackEvents
from tests.knowledge_support import MemoryStorage
from tests.picker_support import ScriptedPicker
from tests.picture_support import FrozenClock

_HTMX = {"HX-Request": "true"}
_BASE = "https://media.example.test"
_MODEL = "google/gemini-3.1-flash-lite-image-20260630"
_RAISE = ExerciseCandidate(id=uuid4(), name="Dumbbell Front Raise", how="Lift them forward.")
_GOBLET = ExerciseCandidate(id=uuid4(), name="Goblet Squat", how="Hold a dumbbell at your chest.")
_CABLE = ExerciseCandidate(id=uuid4(), name="Cable Lateral Raise", how="Lift the handle out.")
_CANDIDATES = (_RAISE, _GOBLET, _CABLE)
_LIVE = (MediaLinkStatus.SUGGESTED, MediaLinkStatus.APPROVED)


class _Catalogue(MemoryExerciseMedia):
    """The shared catalogue in memory: the admin's review port, and the card's pictures and
    drawing ports over the same links, so a takedown or a Restore shows on the next card."""

    def __init__(self) -> None:
        super().__init__()
        self.subjects: dict[UUID, PictureSubject] = {}
        for candidate in _CANDIDATES:
            self.names[candidate.id] = candidate.name
            self.subjects[candidate.id] = PictureSubject(
                exercise_id=candidate.id,
                name=candidate.name,
                sport_id="gym",
                text=ExerciseText(lang="en", instructions=candidate.how or "", steps=()),
            )
        self.public, self.private = MemoryStorage(), MemoryStorage()

    def store(self, exercise_id: UUID, asset: MediaAsset) -> UUID:
        """An approved stored picture, with its file in the public bucket."""
        assert asset.storage_key is not None
        self.public.objects[asset.storage_key] = b"jpeg-bytes"
        self.public.content_types[asset.storage_key] = "image/jpeg"
        return self.put(exercise_id, asset, MediaLinkStatus.APPROVED)

    async def first_approved_pictures(
        self, exercise_ids: Collection[UUID]
    ) -> dict[UUID, MediaAsset]:
        found: dict[UUID, MediaAsset] = {}
        for link in sorted(self.links, key=lambda link: link.position):
            if (
                link.exercise_id in exercise_ids
                and link.status is MediaLinkStatus.APPROVED
                and link.asset.storage_key is not None
            ):
                found.setdefault(link.exercise_id, link.asset)
        return found

    def _drawable(self, exercise_id: UUID) -> bool:
        return exercise_id in self.subjects and not any(
            link.exercise_id == exercise_id
            and (
                link.status in _LIVE
                or (
                    link.status is MediaLinkStatus.REMOVED
                    and link.asset.source == GENERATED_PICTURE_SOURCE
                )
            )
            for link in self.links
        )

    async def drawable(self, exercise_ids: Collection[UUID], *, lang: str) -> set[UUID]:
        return {exercise_id for exercise_id in exercise_ids if self._drawable(exercise_id)}

    async def card_subject(self, exercise_id: UUID, *, lang: str) -> PictureSubject | None:
        return self.subjects[exercise_id] if self._drawable(exercise_id) else None

    async def has_sha256(self, sha256: str) -> bool:
        return any(link.asset.sha256 == sha256 for link in self.links)

    async def add_card_picture(
        self, asset: MediaAsset, exercise_id: UUID, *, lang: str
    ) -> UUID | None:
        if not self._drawable(exercise_id):
            return None
        link = self.put(exercise_id, asset, MediaLinkStatus.APPROVED)
        return self.review_asset_id(link)

    def review_asset_id(self, link_id: UUID) -> UUID:
        [asset_id] = [link.asset.id for link in self.links if link.link_id == link_id]
        assert asset_id is not None
        return asset_id


class _Names:
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
        return _CANDIDATES


def _extraction() -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [{"name": "raises", "sets": 3, "reps": 12}],
        }
    )


class _App:
    def __init__(
        self,
        catalogue: _Catalogue,
        *,
        events: InMemoryFeedbackEvents | None = None,
        clarifications: InMemoryClarificationRepository | None = None,
        flag: bool = True,
    ) -> None:
        app = FastAPI()
        self.catalogue = catalogue
        self.events = events or InMemoryFeedbackEvents()
        self.images = FakeImages()
        self.cdn = FakeCdn()
        self.clarifications = clarifications or InMemoryClarificationRepository()
        profile = InMemoryProfile()
        card_media = ExerciseCardMedia(media=catalogue, public_base_url=_BASE)
        draw = DrawCardPicture(
            CardPicturePorts(
                media=catalogue,
                spend=FakeSpend(),
                briefs=FakeBriefs(),
                brief_check=FakeCheck(),
                images=self.images,
                storage=catalogue.public,
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
            profile=profile,
            clarifications=self.clarifications,
            settlers={
                "gym": ResolveExercises(
                    _Names(),
                    ScriptedPicker(),
                    question=conversation.exercise_question,
                    limits=ResolutionLimits(
                        near_match=0.75, candidate_floor=0.25, shortlist_size=4
                    ),
                )
            },
            card_media=card_media,
            card_pictures=CardPictures(media=catalogue, draw=draw, card_media=card_media),
            card_picture_flag=(
                CardPictureFlag(
                    media=catalogue,
                    takedown=MediaTakedown(
                        media=catalogue,
                        quarantine=MediaQuarantine(
                            media=catalogue.public, uploads=catalogue.private
                        ),
                        clock=ReviewClock(),
                        public_base_url=_BASE,
                        cdn=self.cdn,
                    ),
                    feedback=FeedbackRecorder(self.events, clock=FixedClock()),
                )
                if flag
                else None
            ),
        )
        self.user = sign_in_as(app)
        profile.sports[self.user.id] = ["gym"]
        self.client = TestClient(app)

    async def reply(self) -> str:
        message = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            "raises 3x12",
            FROZEN_NOW,
        )
        return " ".join(self.client.get(f"/messages/{message.id}/reply").text.split())

    def open_question(self) -> UUID:
        [question] = [
            row.id for row in self.clarifications.all_of(self.user.id) if row.status == "open"
        ]
        return question

    def flag(self, question: UUID, option: str) -> tuple[int, str]:
        response = self.client.post(
            f"/clarifications/{question}/picture/{option}/flag", headers=_HTMX
        )
        return response.status_code, " ".join(response.text.split())


def _candidate(html: str, candidate: ExerciseCandidate) -> str:
    """The candidate's `group/cand` wrapper, up to the next candidate or "Keep my name"."""
    start = html.rindex("<", 0, html.index(f'data-candidate="{candidate.id}"'))
    following = html.find("data-choice-option=", html.index(f'data-choice-option="{candidate.id}"'))
    following = html.find("data-choice-option=", following + 1)
    end = html.rindex("<", 0, following) if following != -1 else len(html)
    return html[start:end]


def _drawn() -> tuple[_Catalogue, UUID, MediaAsset]:
    """A catalogue whose Dumbbell Front Raise has a picture the card drew, a wger picture on the
    Goblet Squat and an admin-approved generated picture on the Cable Lateral Raise."""
    catalogue = _Catalogue()
    asset = card_drawn_asset()
    link = catalogue.store(_RAISE.id, asset)
    catalogue.store(_GOBLET.id, wger_asset())
    catalogue.store(_CABLE.id, generated_asset())
    return catalogue, link, asset


async def test_drawn_picture_has_the_flag_on_its_credit_line() -> None:
    catalogue, _, asset = _drawn()
    app = _App(catalogue)

    html = await app.reply()

    question = app.open_question()
    block = _candidate(html, _RAISE)
    assert f'<img src="{_BASE}/{asset.storage_key}"' in block
    [button] = re.findall(r"<button[^>]*data-picture-flag[^>]*>", block)
    assert f'hx-post="/clarifications/{question}/picture/{_RAISE.id}/flag"' in button
    assert 'aria-label="Looks wrong: hide this picture"' in button
    assert 'hx-target="closest [data-candidate]"' in button
    assert 'hx-swap="outerHTML"' in button
    assert 'hx-status:4xx="swap:none"' in button
    assert "size-7 rounded-full" in button
    assert '<span class="absolute -inset-2" aria-hidden="true"></span>' in block
    assert "pr-12" in block
    # A sibling of the row button, never inside it, so a tap never answers the question.
    row = block[: block.index("</button>")]
    assert "data-picture-flag" not in row


async def test_a_picture_the_slot_just_drew_comes_with_its_flag() -> None:
    """#641's "picture: arrived": the slot's answer carries the flag on its out-of-band credit."""
    catalogue = _Catalogue()
    app = _App(catalogue)
    html = await app.reply()
    question = app.open_question()
    assert "data-picture-load" in _candidate(html, _RAISE)

    response = app.client.post(f"/clarifications/{question}/picture/{_RAISE.id}", headers=_HTMX)

    body = " ".join(response.text.split())
    credit = body[body.index(f'id="choice-{question}-credit-{_RAISE.id}"') :]
    assert 'hx-swap-oob="true"' in credit
    assert f'hx-post="/clarifications/{question}/picture/{_RAISE.id}/flag"' in credit
    assert "pr-12" in credit


async def test_without_the_flag_service_a_just_drawn_picture_has_no_flag() -> None:
    catalogue = _Catalogue()
    app = _App(catalogue, flag=False)
    await app.reply()
    question = app.open_question()

    response = app.client.post(f"/clarifications/{question}/picture/{_RAISE.id}", headers=_HTMX)

    assert "<img" in response.text
    assert "data-picture-flag" not in response.text


# AC 1: flagging hides the picture on the next card for every athlete.
async def test_flag_hides_the_picture_for_every_athlete() -> None:
    catalogue, _, asset = _drawn()
    first = _App(catalogue)
    await first.reply()
    question = first.open_question()

    status, body = first.flag(question, str(_RAISE.id))

    assert status == 200
    assert body.startswith('<div class="group/cand')
    assert f'data-candidate="{_RAISE.id}"' in body
    assert "<img" not in body
    assert "data-picture-flag" not in body
    assert "data-picture-load" not in body
    assert 'role="status" data-flagged="picture"' in body
    assert "Thanks, the picture is hidden." in body
    # The flag never answers the question.
    assert first.open_question() == question
    second = _App(catalogue)

    html = await second.reply()

    block = _candidate(html, _RAISE)
    assert f"{asset.storage_key}" not in block
    assert "<img" not in block
    # A removed generated picture is never drawn again: no loading slot, no model call.
    assert "data-picture-load" not in block
    assert second.images.calls == []


# AC 3: one content-free `picture_flagged` event.
async def test_flag_records_one_content_free_event() -> None:
    catalogue, _, _ = _drawn()
    app = _App(catalogue)
    await app.reply()
    question = app.open_question()

    app.flag(question, str(_RAISE.id))
    status, body = app.flag(question, str(_RAISE.id))

    [event] = app.events.events
    assert (event.user_id, event.kind) == (app.user.id, FeedbackKind.PICTURE_FLAGGED)
    assert (event.template_id, event.template_version, tuple(event.fields)) == (None, None, ())
    # A second tap (a stale card) gets the same answer and records nothing.
    assert status == 200
    assert "Thanks, the picture is hidden." in body


# AC 2 through the route: the file leaves the public bucket and its URL is purged.
async def test_flag_route_quarantines_the_file_and_purges_its_url() -> None:
    catalogue, _, asset = _drawn()
    app = _App(catalogue)
    await app.reply()

    app.flag(app.open_question(), str(_RAISE.id))

    assert asset.storage_key not in catalogue.public.objects
    assert f"quarantine/{asset.storage_key}" in catalogue.private.objects
    assert app.cdn.purged == [f"{_BASE}/{asset.storage_key}"]


# AC 4: the admin sees it under Removed, and Restore shows it again.
async def test_admin_restore_shows_the_flagged_picture_again() -> None:
    catalogue, link, asset = _drawn()
    first = _App(catalogue)
    await first.reply()
    first.flag(first.open_question(), str(_RAISE.id))
    review = review_with_takedown(
        catalogue,
        quarantine=MediaQuarantine(media=catalogue.public, uploads=catalogue.private),
        clock=ReviewClock(),
        base_url=_BASE,
    )

    page = await review.page(MediaLinkStatus.REMOVED)
    [group] = page.groups
    [picture] = group.pictures
    assert (group.exercise_id, picture.link_id) == (_RAISE.id, link)
    assert picture.removed_from is MediaLinkStatus.APPROVED
    assert picture.origin == "generated"

    assert await review.restore(link) == Restored(
        exercise_name=_RAISE.name, status=MediaLinkStatus.APPROVED
    )

    html = await _App(catalogue).reply()
    block = _candidate(html, _RAISE)
    assert f'<img src="{_BASE}/{asset.storage_key}"' in block
    assert "data-picture-flag" in block


# Edge: a wger picture and an admin-approved generated one have no flag.
async def test_wger_and_admin_generated_pictures_have_no_flag() -> None:
    catalogue, _, _ = _drawn()
    app = _App(catalogue)

    html = await app.reply()

    for candidate in (_GOBLET, _CABLE):
        block = _candidate(html, candidate)
        assert "<img" in block
        assert "data-picture-flag" not in block
        assert "pr-12" not in block


async def test_flag_route_refuses_a_picture_not_drawn_on_the_card() -> None:
    catalogue, _, _ = _drawn()
    app = _App(catalogue)
    await app.reply()
    question = app.open_question()

    for candidate in (_GOBLET, _CABLE):
        status, body = app.flag(question, str(candidate.id))

        assert status == 200
        assert "<img" in body
        assert "Thanks, the picture is hidden." not in body
    assert app.events.events == []
    assert catalogue.private.objects == {}
    assert all(link.status is MediaLinkStatus.APPROVED for link in catalogue.links)


async def test_without_the_flag_service_there_is_no_flag_and_the_route_is_404() -> None:
    catalogue, _, _ = _drawn()
    app = _App(catalogue, flag=False)

    html = await app.reply()

    assert "data-picture-flag" not in html
    assert app.flag(app.open_question(), str(_RAISE.id))[0] == 404
    assert all(link.status is MediaLinkStatus.APPROVED for link in catalogue.links)


async def test_another_users_question_is_404() -> None:
    catalogue, _, _ = _drawn()
    app = _App(catalogue)
    await app.reply()
    stranger = _App(catalogue, clarifications=app.clarifications)

    assert stranger.flag(app.open_question(), str(_RAISE.id))[0] == 404
    assert stranger.events.events == []
    assert all(link.status is MediaLinkStatus.APPROVED for link in catalogue.links)


async def test_unknown_question_or_option_is_404() -> None:
    catalogue, _, _ = _drawn()
    app = _App(catalogue)
    await app.reply()
    question = app.open_question()

    for option in (str(uuid4()), "keep", "not-an-id"):
        assert app.flag(question, option)[0] == 404
    assert app.flag(uuid4(), str(_RAISE.id))[0] == 404
    assert app.events.events == []


async def test_answered_question_is_409() -> None:
    catalogue, _, _ = _drawn()
    app = _App(catalogue)
    await app.reply()
    question = app.open_question()
    app.client.post(
        f"/clarifications/{question}/answer",
        data={"option_ids": str(_GOBLET.id)},
        headers=_HTMX,
    )

    assert app.flag(question, str(_RAISE.id))[0] == 409
    assert app.events.events == []
