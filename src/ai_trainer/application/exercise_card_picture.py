"""The picture drawn on the exercise card (ADR-0031 *Picture on the card*, #643).

When the card shows a drawable catalogue candidate, its picture slot asks for one. The brief is
written from the exercise's `exercise_texts` only (no `search_knowledge` on this path), Jev checks
it, the image model draws it, the bytes go to the media bucket and the link is written `approved`,
first write wins. Every call is the athlete's whose card asked, through the same model-call budget
(ADR-0029), and `IMAGE_GENERATION_DAILY_CAP_USD` bounds every athlete together per UTC day.
Every fallback stores nothing and leaves the row text-only, so the next card tries again; its log
line names the reason and the exercise ID only."""

import contextlib
import hashlib
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import Decimal
from uuid import UUID

from ai_trainer.application.exercise_card_media import CardPicture, ExerciseCardMedia
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.llm_calls import PictureDaySpendPort
from ai_trainer.application.ports.media import CardPictureRepositoryPort
from ai_trainer.application.ports.media_generation import (
    ImageGenerationPort,
    PictureBriefCheckPort,
    PictureBriefWriterPort,
)
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.media import MediaRepositoryError
from ai_trainer.domain.media_generation import (
    BriefCheckRefused,
    BriefFailed,
    CardPictureDrawn,
    CardPictureFailure,
    CardPictureSkipped,
    GeneratedImage,
    ImageGenerationFailed,
    NoBrief,
    PictureBrief,
    PictureSubject,
    card_picture_asset,
    exercise_description,
    picture_brief_deps,
)
from ai_trainer.domain.object_storage import ObjectStorageError

logger = logging.getLogger(__name__)

# `media.picture_brief` v1 and `picture_brief_check` read English, and the ADR asks for an
# English `exercise_texts` row.
_TEXT_LANG = "en"

type CardPictureResult = CardPictureDrawn | CardPictureSkipped


@dataclass(frozen=True, slots=True)
class CardPictureLimits:
    """`EXERCISE_CARD_PICTURES_ENABLED`, `IMAGE_GENERATION_DAILY_CAP_USD` and the picture's price
    `IMAGE_GENERATION_PRICE_USD`, which only checks the cap (ADR-0031)."""

    enabled: bool
    daily_cap_usd: Decimal
    price_usd: Decimal


@dataclass(frozen=True, slots=True)
class CardPicturePorts:
    media: CardPictureRepositoryPort
    spend: PictureDaySpendPort
    briefs: PictureBriefWriterPort
    brief_check: PictureBriefCheckPort
    images: ImageGenerationPort
    storage: ObjectStoragePort


class DrawCardPicture:
    def __init__(
        self,
        ports: CardPicturePorts,
        *,
        limits: CardPictureLimits,
        clock: ClockPort,
        model: str,
    ) -> None:
        self.ports = ports
        self._limits = limits
        self._clock = clock
        self._model = model

    @property
    def enabled(self) -> bool:
        return self._limits.enabled

    async def draw(self, *, user_id: UUID, exercise_id: UUID) -> CardPictureResult:
        result = await self._draw(user_id, exercise_id)
        if isinstance(result, CardPictureSkipped):
            logger.info("card picture not drawn: reason=%s exercise=%s", result.reason, exercise_id)
        return result

    async def _draw(self, user_id: UUID, exercise_id: UUID) -> CardPictureResult:
        if not self._limits.enabled:
            return CardPictureSkipped(reason="switch_off")
        subject = await self.ports.media.card_subject(exercise_id, lang=_TEXT_LANG)
        instructions = None if subject is None else exercise_description(subject.text)
        if subject is None or instructions is None:
            return CardPictureSkipped(reason="not_drawable")
        # A day at the cap makes no call at all.
        if await self._spent_today() >= self._limits.daily_cap_usd:
            return CardPictureSkipped(reason="daily_cap")
        brief = await self._brief(user_id, subject, instructions)
        if isinstance(brief, CardPictureSkipped):
            return brief
        # The image is skipped when its price would take today's spend over the cap.
        if await self._spent_today() + self._limits.price_usd > self._limits.daily_cap_usd:
            return CardPictureSkipped(reason="daily_cap")
        image = await self.ports.images.generate(user_id=user_id, prompt=brief.image_prompt)
        if isinstance(image, ImageGenerationFailed):
            return CardPictureSkipped(reason=_image_failure(image))
        return await self._store(exercise_id, image)

    async def _spent_today(self) -> Decimal:
        now = self._clock.now().astimezone(UTC)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.ports.spend.spent_between(
            start, start + timedelta(days=1), picture_price=self._limits.price_usd
        )

    async def _brief(
        self, user_id: UUID, subject: PictureSubject, instructions: str
    ) -> PictureBrief | CardPictureSkipped:
        brief = await self.ports.briefs.write(
            user_id=user_id, deps=picture_brief_deps(subject.name, subject.text)
        )
        if isinstance(brief, NoBrief):
            return CardPictureSkipped(reason="no_brief")
        if isinstance(brief, BriefFailed):
            return CardPictureSkipped(reason=_brief_failure(brief))
        checked = await self.ports.brief_check.check(
            user_id=user_id, instructions=instructions, brief=brief.image_prompt
        )
        if isinstance(checked, BriefCheckRefused):
            return CardPictureSkipped(reason="rate_limited")
        if not checked.passed:
            return CardPictureSkipped(reason="brief_check_failed")
        return brief

    async def _store(self, exercise_id: UUID, image: GeneratedImage) -> CardPictureResult:
        media = self.ports.media
        sha256 = hashlib.sha256(image.data).hexdigest()
        # The same bytes are one asset; its object under that key is kept as it is.
        if await media.has_sha256(sha256):
            return CardPictureSkipped(reason="already_stored")
        asset = card_picture_asset(
            exercise_id=exercise_id,
            image=image,
            model=self._model,
            generated_on=self._clock.now().astimezone(UTC).date(),
        )
        key = asset.storage_key
        if key is None:  # pragma: no cover -- a stored asset always has its key
            raise ValueError("stored media has a storage key")
        try:
            await self.ports.storage.put(key, image.data, content_type=image.media_type)
        except ObjectStorageError:
            return CardPictureSkipped(reason="storage_error")
        try:
            asset_id = await media.add_card_picture(asset, exercise_id, lang=_TEXT_LANG)
        except MediaRepositoryError:
            await self._discard(key)
            return CardPictureSkipped(reason="database_error")
        except BaseException:
            # Anything else, and a cancelled request, still leaves no object without a row.
            await self._discard(key)
            raise
        if asset_id is None:
            # Another card wrote first. Its object shares this key only when its bytes are the
            # same, and then it is the winner's to keep.
            if not await media.has_sha256(sha256):
                await self._discard(key)
            return CardPictureSkipped(reason="already_drawn")
        return CardPictureDrawn(asset_id=asset_id)

    async def _discard(self, key: str) -> None:
        with contextlib.suppress(ObjectStorageError):
            await self.ports.storage.delete(key)


def _brief_failure(failed: BriefFailed) -> CardPictureFailure:
    match failed.reason:
        case "timeout":
            return "brief_timeout"
        case "error":
            return "brief_error"
        case "rate_limited":
            return "rate_limited"


def _image_failure(failed: ImageGenerationFailed) -> CardPictureFailure:
    match failed.reason:
        case "timeout":
            return "image_timeout"
        case "error":
            return "image_error"
        case "rate_limited":
            return "rate_limited"


# A picture is (or may now be) on the exercise, so the card re-reads it: the winner's after a
# lost race, or one another card drew since this card was rendered.
_SHOW_AFTER: frozenset[CardPictureFailure] = frozenset({"already_drawn", "not_drawable"})


class CardPictures:
    """Facade for the chat's exercise card: which candidates get a loading slot, and the slot's
    picture once drawn."""

    def __init__(
        self,
        *,
        media: CardPictureRepositoryPort,
        draw: DrawCardPicture,
        card_media: ExerciseCardMedia,
    ) -> None:
        self._media = media
        self._draw = draw
        self._card_media = card_media

    async def drawable(self, exercise_ids: Collection[UUID]) -> set[UUID]:
        """Read in the same render as the card's pictures; nothing while the switch is off."""
        if not self._draw.enabled or not exercise_ids:
            return set()
        return await self._media.drawable(exercise_ids, lang=_TEXT_LANG)

    async def load(self, *, user_id: UUID, exercise_id: UUID) -> CardPicture | None:
        """The exercise's picture after drawing it, or None: the row stays text-only."""
        result = await self._draw.draw(user_id=user_id, exercise_id=exercise_id)
        if isinstance(result, CardPictureSkipped) and result.reason not in _SHOW_AFTER:
            return None
        pictures = await self._card_media.pictures([exercise_id])
        return pictures.get(exercise_id)
