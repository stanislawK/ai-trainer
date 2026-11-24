"""Fakes for the picture drawn on the exercise card (ADR-0031 *Picture on the card*, #643): the
card's media tables, the day's picture spend, the brief writer, Jev's brief check and the image
model, each counting its calls. No model is called."""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from ai_trainer.domain.media import MediaAsset, MediaRepositoryError
from ai_trainer.domain.media_generation import (
    BriefCheck,
    BriefCheckRefused,
    BriefFailed,
    GeneratedImage,
    ImageGenerationFailed,
    NoBrief,
    PictureBrief,
    PictureBriefDeps,
    PictureSubject,
)

CARD_BRIEF = PictureBrief(
    exercise_name="Pull-Up",
    start_position="Hanging, arms extended",
    end_position="Chest at the bar",
    image_prompt="One person hanging, then chest at the bar. Plain background. No text.",
)
CARD_IMAGE = GeneratedImage(data=b"\xff\xd8\xffjpeg-bytes", media_type="image/jpeg")


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
    reply: PictureBrief | NoBrief | BriefFailed = field(default_factory=lambda: CARD_BRIEF)
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
    reply: GeneratedImage | ImageGenerationFailed = CARD_IMAGE
    calls: list[tuple[UUID, str]] = field(default_factory=list)

    async def generate(
        self, *, user_id: UUID, prompt: str
    ) -> GeneratedImage | ImageGenerationFailed:
        self.calls.append((user_id, prompt))
        return self.reply
