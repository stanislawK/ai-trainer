"""The picture drawn on the exercise card (ADR-0031 *Picture on the card*, #643): a drawable
catalogue candidate gets a brief from its text only, Jev's check, a picture, the media bucket and
an `approved` link, under the athlete whose card asked. Every fallback stores nothing and the next
card tries again; a writer that loses the race discards its own object. Every port is a fake: no
model is called."""

import hashlib
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.exercise_card_media import ExerciseCardMedia
from ai_trainer.application.exercise_card_picture import (
    CardPictureLimits,
    CardPicturePorts,
    CardPictures,
    DrawCardPicture,
)
from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.media import MediaAsset, MediaRepositoryError
from ai_trainer.domain.media_generation import (
    CARD_PICTURE_FRAGMENT,
    GENERATED_PICTURE_SOURCE,
    BriefCheck,
    BriefCheckRefused,
    BriefFailed,
    CardPictureDrawn,
    CardPictureSkipped,
    GeneratedImage,
    ImageGenerationFailed,
    NoBrief,
    PictureBrief,
    PictureBriefDeps,
    PictureSubject,
)
from tests.picture_support import FROZEN, FrozenClock, InMemoryBucket

_USER = uuid4()
_EXERCISE = uuid4()
_TEXT = ExerciseText(
    lang="en", instructions="Hang from the bar, then pull your chest up to it.", steps=()
)
_SUBJECT = PictureSubject(exercise_id=_EXERCISE, name="Pull-Up", sport_id="gym", text=_TEXT)
_BRIEF = PictureBrief(
    exercise_name="Pull-Up",
    start_position="Hanging, arms extended",
    end_position="Chest at the bar",
    image_prompt="One person hanging, then chest at the bar. Plain background. No text.",
)
_IMAGE = GeneratedImage(data=b"\xff\xd8\xffjpeg-bytes", media_type="image/jpeg")
_PRICE = Decimal("0.0336")
_CAP = Decimal("1.00")
_MODEL = "google/gemini-3.1-flash-lite-image-20260630"
_BASE = "https://media.example.test"


@dataclass
class FakeCardMedia:
    """Implements `CardPictureRepositoryPort` and `ExerciseCardMediaPort` in memory."""

    subjects: dict[UUID, PictureSubject] = field(default_factory=dict)
    shas: set[str] = field(default_factory=set)
    approved: dict[UUID, MediaAsset] = field(default_factory=dict)
    lose: bool = False
    fail: bool = False
    # When the loser's write is refused, the winner's bytes may already be catalogued.
    winner_shas: set[str] = field(default_factory=set)
    adds: list[tuple[MediaAsset, UUID]] = field(default_factory=list)
    drawable_reads: list[set[UUID]] = field(default_factory=list)

    async def drawable(self, exercise_ids: Collection[UUID], *, lang: str) -> set[UUID]:
        self.drawable_reads.append(set(exercise_ids))
        return {
            exercise_id
            for exercise_id in exercise_ids
            if exercise_id in self.subjects and exercise_id not in self.approved
        }

    async def card_subject(self, exercise_id: UUID, *, lang: str) -> PictureSubject | None:
        if exercise_id in self.approved:
            return None
        return self.subjects.get(exercise_id)

    async def has_sha256(self, sha256: str) -> bool:
        return sha256 in self.shas

    async def add_card_picture(
        self, asset: MediaAsset, exercise_id: UUID, *, lang: str
    ) -> UUID | None:
        if self.fail:
            raise MediaRepositoryError
        if self.lose:
            self.shas |= self.winner_shas
            return None
        self.adds.append((asset, exercise_id))
        asset_id = uuid4()
        self.approved[exercise_id] = asset.model_copy(update={"id": asset_id})
        assert asset.sha256 is not None
        self.shas.add(asset.sha256)
        return asset_id

    async def first_approved_pictures(
        self, exercise_ids: Collection[UUID]
    ) -> dict[UUID, MediaAsset]:
        return {key: value for key, value in self.approved.items() if key in exercise_ids}


@dataclass
class FakeSpend:
    """Implements `PictureDaySpendPort`: one figure per read, the last one repeated."""

    spent: list[Decimal] = field(default_factory=lambda: [Decimal(0)])
    reads: list[tuple[datetime, datetime, Decimal]] = field(default_factory=list)

    async def spent_between(
        self, start: datetime, end: datetime, *, picture_price: Decimal
    ) -> Decimal:
        self.reads.append((start, end, picture_price))
        return self.spent[min(len(self.reads), len(self.spent)) - 1]


@dataclass
class FakeBriefs:
    reply: PictureBrief | NoBrief | BriefFailed = field(default_factory=lambda: _BRIEF)
    calls: list[tuple[UUID, PictureBriefDeps]] = field(default_factory=list)

    async def write(
        self, *, user_id: UUID, deps: PictureBriefDeps
    ) -> PictureBrief | NoBrief | BriefFailed:
        self.calls.append((user_id, deps))
        return self.reply


@dataclass
class FakeCheck:
    reply: BriefCheck | BriefCheckRefused = field(
        default_factory=lambda: BriefCheck(passed=True, version=1)
    )
    calls: list[tuple[UUID, str, str]] = field(default_factory=list)

    async def check(
        self, *, user_id: UUID, instructions: str, brief: str
    ) -> BriefCheck | BriefCheckRefused:
        self.calls.append((user_id, instructions, brief))
        return self.reply


@dataclass
class FakeImages:
    reply: GeneratedImage | ImageGenerationFailed = _IMAGE
    calls: list[tuple[UUID, str]] = field(default_factory=list)

    async def generate(
        self, *, user_id: UUID, prompt: str
    ) -> GeneratedImage | ImageGenerationFailed:
        self.calls.append((user_id, prompt))
        return self.reply


class _Card:
    def __init__(self, *, enabled: bool = True) -> None:
        self.media = FakeCardMedia(subjects={_EXERCISE: _SUBJECT})
        self.spend = FakeSpend()
        self.briefs = FakeBriefs()
        self.check = FakeCheck()
        self.images = FakeImages()
        self.bucket = InMemoryBucket()
        self.draw = DrawCardPicture(
            CardPicturePorts(
                media=self.media,
                spend=self.spend,
                briefs=self.briefs,
                brief_check=self.check,
                images=self.images,
                storage=self.bucket,
            ),
            limits=CardPictureLimits(enabled=enabled, daily_cap_usd=_CAP, price_usd=_PRICE),
            clock=FrozenClock(),
            model=_MODEL,
        )
        self.pictures = CardPictures(
            media=self.media,
            draw=self.draw,
            card_media=ExerciseCardMedia(media=self.media, public_base_url=_BASE),
        )

    @property
    def model_calls(self) -> int:
        return len(self.briefs.calls) + len(self.check.calls) + len(self.images.calls)


# AC 1: a drawable candidate gets its picture, written `approved` with the `#card` marker.
async def test_draw_writes_approved_card_link_and_returns_picture() -> None:
    card = _Card()

    picture = await card.pictures.load(user_id=_USER, exercise_id=_EXERCISE)

    [(asset, exercise_id)] = card.media.adds
    assert exercise_id == _EXERCISE
    assert asset.source == GENERATED_PICTURE_SOURCE
    assert asset.source_ref == _MODEL + CARD_PICTURE_FRAGMENT
    assert asset.attribution is not None
    assert asset.attribution.endswith("· 2026-11-11")
    assert asset.storage_key is not None
    assert card.bucket.objects[asset.storage_key] == _IMAGE.data
    assert picture is not None
    assert picture.public_url == f"{_BASE}/{asset.storage_key}"
    assert picture.origin == "generated"
    assert picture.drawn_on_card
    # Every call is the athlete's, and the brief is written from the catalogue text only.
    [(brief_user, deps)] = card.briefs.calls
    assert brief_user == _USER
    assert deps == PictureBriefDeps(
        exercise_name="Pull-Up", description=_TEXT.instructions, passages=[]
    )
    assert card.check.calls == [(_USER, _TEXT.instructions, _BRIEF.image_prompt)]
    assert card.images.calls == [(_USER, _BRIEF.image_prompt)]


async def test_the_daily_cap_reads_today_in_utc_with_the_picture_price() -> None:
    card = _Card()

    await card.draw.draw(user_id=_USER, exercise_id=_EXERCISE)

    start = FROZEN.replace(hour=0, minute=0, second=0, microsecond=0)
    assert [
        (read_start, read_end - read_start) for read_start, read_end, _ in card.spend.reads
    ] == [(start, start.replace(day=12) - start)] * 2
    assert {price for _, _, price in card.spend.reads} == {_PRICE}


def _setup(case: str, card: _Card) -> None:
    match case:
        case "switch_off":
            card.draw = DrawCardPicture(
                card.draw.ports,
                limits=CardPictureLimits(enabled=False, daily_cap_usd=_CAP, price_usd=_PRICE),
                clock=FrozenClock(),
                model=_MODEL,
            )
        case "not_drawable":
            card.media.subjects.clear()
        case "no_description":
            card.media.subjects[_EXERCISE] = PictureSubject(
                exercise_id=_EXERCISE,
                name="Pull-Up",
                sport_id="gym",
                text=ExerciseText(lang="en", instructions=" ", steps=()),
            )
        case "daily_cap_before_brief":
            card.spend.spent = [_CAP]
        case "daily_cap_before_image":
            card.spend.spent = [Decimal(0), _CAP - _PRICE + Decimal("0.0001")]
        case "brief_timeout":
            card.briefs.reply = BriefFailed(reason="timeout")
        case "brief_error":
            card.briefs.reply = BriefFailed(reason="error")
        case "budget_refused_brief":
            card.briefs.reply = BriefFailed(reason="rate_limited")
        case "no_brief":
            card.briefs.reply = NoBrief(reason="no positions")
        case "check_under_threshold":
            card.check.reply = BriefCheck(passed=False, version=1)
        case "check_refused":
            card.check.reply = BriefCheckRefused()
        case "image_timeout":
            card.images.reply = ImageGenerationFailed(reason="timeout")
        case "image_error":
            card.images.reply = ImageGenerationFailed(reason="error", cause="http_status")
        case "budget_refused_image":
            card.images.reply = ImageGenerationFailed(reason="rate_limited")
        case "store_failed":
            card.bucket.fail_put = True
        case "write_failed":
            card.media.fail = True
        case "already_stored":
            card.media.shas.add(_sha(_IMAGE))
        case _:
            raise AssertionError(case)


def _sha(image: GeneratedImage) -> str:
    return hashlib.sha256(image.data).hexdigest()


# AC 3: a failed check, timeout, budget refusal or daily cap leaves the row text-only.
@pytest.mark.parametrize(
    ("case", "reason", "model_calls"),
    [
        ("switch_off", "switch_off", 0),
        ("not_drawable", "not_drawable", 0),
        ("no_description", "not_drawable", 0),
        ("daily_cap_before_brief", "daily_cap", 0),
        ("brief_timeout", "brief_timeout", 1),
        ("brief_error", "brief_error", 1),
        ("budget_refused_brief", "rate_limited", 1),
        ("no_brief", "no_brief", 1),
        ("check_under_threshold", "brief_check_failed", 2),
        ("check_refused", "rate_limited", 2),
        ("daily_cap_before_image", "daily_cap", 2),
        ("image_timeout", "image_timeout", 3),
        ("image_error", "image_error", 3),
        ("budget_refused_image", "rate_limited", 3),
        ("already_stored", "already_stored", 3),
        ("store_failed", "storage_error", 3),
        ("write_failed", "database_error", 3),
    ],
)
async def test_fallback_stores_nothing(
    case: str, reason: str, model_calls: int, caplog: pytest.LogCaptureFixture
) -> None:
    card = _Card()
    _setup(case, card)
    caplog.set_level("INFO")

    result = await card.draw.draw(user_id=_USER, exercise_id=_EXERCISE)

    assert result == CardPictureSkipped(reason=reason)  # type: ignore[arg-type]  # str from params
    assert card.model_calls == model_calls
    assert card.media.adds == []
    assert card.bucket.objects == {}
    # The log names the reason and the exercise ID only.
    [record] = [
        r for r in caplog.records if r.name == "ai_trainer.application.exercise_card_picture"
    ]
    assert record.getMessage() == f"card picture not drawn: reason={reason} exercise={_EXERCISE}"


async def test_already_stored_keeps_the_existing_object() -> None:
    card = _Card()
    _setup("already_stored", card)
    card.bucket.objects["kept"] = b"x"

    await card.draw.draw(user_id=_USER, exercise_id=_EXERCISE)

    assert card.bucket.objects == {"kept": b"x"}


async def test_a_cancelled_write_discards_the_object() -> None:
    card = _Card()

    async def cancelled(asset: MediaAsset, exercise_id: UUID, *, lang: str) -> UUID | None:
        raise KeyboardInterrupt

    card.media.add_card_picture = cancelled  # type: ignore[method-assign]  # scripted failure

    with pytest.raises(KeyboardInterrupt):
        await card.draw.draw(user_id=_USER, exercise_id=_EXERCISE)

    assert card.bucket.objects == {}


async def test_next_card_retries_after_fallback() -> None:
    card = _Card()
    card.images.reply = ImageGenerationFailed(reason="timeout")
    assert await card.pictures.load(user_id=_USER, exercise_id=_EXERCISE) is None
    assert await card.pictures.drawable([_EXERCISE]) == {_EXERCISE}

    card.images.reply = _IMAGE
    picture = await card.pictures.load(user_id=_USER, exercise_id=_EXERCISE)

    assert picture is not None
    assert len(card.images.calls) == 2
    assert await card.pictures.drawable([_EXERCISE]) == set()


# AC 4: a writer that loses the race discards its own object and shows the winner's picture.
async def test_losing_writer_discards_its_object() -> None:
    card = _Card()
    card.media.lose = True

    result = await card.draw.draw(user_id=_USER, exercise_id=_EXERCISE)

    assert result == CardPictureSkipped(reason="already_drawn")
    assert card.bucket.objects == {}


async def test_losing_writer_keeps_an_object_the_winner_wrote_with_the_same_bytes() -> None:
    card = _Card()
    card.media.lose = True
    card.media.winner_shas = {_sha(_IMAGE)}

    await card.draw.draw(user_id=_USER, exercise_id=_EXERCISE)

    assert list(card.bucket.objects.values()) == [_IMAGE.data]


async def test_loser_shows_the_winners_picture() -> None:
    card = _Card()
    winner = MediaAsset.model_validate(
        {
            "id": uuid4(),
            "kind": "image",
            "rights": "licensed",
            "delivery": "stored",
            "storage_key": f"exercises/{_EXERCISE}/{'b' * 64}.png",
            "source": GENERATED_PICTURE_SOURCE,
            "source_ref": _MODEL + CARD_PICTURE_FRAGMENT,
            "license": "licence",
            "attribution": "AI generated",
            "sha256": "b" * 64,
        }
    )
    card.media.lose = True
    original = card.media.add_card_picture

    async def lose(asset: MediaAsset, exercise_id: UUID, *, lang: str) -> UUID | None:
        card.media.approved[exercise_id] = winner
        return await original(asset, exercise_id, lang=lang)

    card.media.add_card_picture = lose  # type: ignore[method-assign]  # the winner writes first

    picture = await card.pictures.load(user_id=_USER, exercise_id=_EXERCISE)

    assert picture is not None
    assert picture.public_url.endswith(f"{'b' * 64}.png")


# AC 5: a custom exercise, or a candidate with a picture, makes no call.
async def test_not_drawable_makes_no_call() -> None:
    card = _Card()
    custom = uuid4()

    assert await card.pictures.drawable([custom]) == set()
    assert await card.pictures.load(user_id=_USER, exercise_id=custom) is None
    assert card.model_calls == 0
    assert card.spend.reads == []


async def test_drawable_reads_nothing_when_switched_off_or_given_no_ids() -> None:
    off = _Card(enabled=False)
    on = _Card()

    assert await off.pictures.drawable([_EXERCISE]) == set()
    assert await on.pictures.drawable([]) == set()
    assert off.media.drawable_reads == []
    assert on.media.drawable_reads == []


async def test_drawn_result_names_the_asset() -> None:
    card = _Card()

    result = await card.draw.draw(user_id=_USER, exercise_id=_EXERCISE)

    assert isinstance(result, CardPictureDrawn)
    assert card.media.approved[_EXERCISE].id == result.asset_id
