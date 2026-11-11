"""Generating one exercise picture (ADR-0031 *Picture generation*, #532, #533): the brief a text
model writes from an exercise's public texts, the picture an image model draws from it, and how
a run ends.

A brief's input is public text only, never athlete data: the catalogue's `exercise_texts` and the
passages the run's own `search_knowledge` returned. An exercise with neither gets no brief, and
no model call."""

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.knowledge_search import KnowledgeSearchFailed, KnowledgeSearchPage
from ai_trainer.domain.media import MediaAsset, MediaDelivery, MediaKind, MediaRights


class BriefPassage(BaseModel):
    """One knowledge passage about the exercise, as the template sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    text: str


class PictureBriefDeps(BaseModel):
    """The template's variables: the exercise's name and public texts, nothing else. Extra
    fields are refused, so no identity can ride along (ADR-0031)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exercise_name: str
    description: str | None
    passages: list[BriefPassage]


class PictureBrief(BaseModel):
    """What the image model is given: the two positions, and the prompt that restates them
    with the drawing rules."""

    exercise_name: str
    start_position: str
    end_position: str
    image_prompt: str


class NoBrief(BaseModel):
    """The texts don't say how the exercise is done, so nothing is drawn."""

    reason: str


def _description(text: ExerciseText | None) -> str | None:
    if text is None:
        return None
    if text.instructions.strip():
        return text.instructions.strip()
    return "\n".join(step.strip() for step in text.steps if step.strip()) or None


def picture_brief_deps(
    exercise_name: str,
    text: ExerciseText | None,
    search: KnowledgeSearchPage | KnowledgeSearchFailed,
) -> PictureBriefDeps:
    """The deps for one exercise: its catalogue text, and only the passages its own search
    returned, best first. A failed search gives none."""
    hits = search.hits if isinstance(search, KnowledgeSearchPage) else ()
    return PictureBriefDeps(
        exercise_name=exercise_name,
        description=_description(text),
        # A passage with no text says nothing, so it can't stand in for a description.
        passages=[BriefPassage(title=hit.title, text=hit.text) for hit in hits if hit.text.strip()],
    )


def needs_no_model(deps: PictureBriefDeps) -> NoBrief | None:
    """`NoBrief` when there is nothing to draw from but the name (ADR-0031): no model is
    called then. `None` when the template should run."""
    if (deps.description or "").strip() or deps.passages:
        return None
    return NoBrief(reason="no exercise text and no knowledge passage")


@dataclass(frozen=True, slots=True)
class PictureSubject:
    """A catalogue exercise to draw: its ID, name and sport, and its text in the language asked
    for, else in any language, else none."""

    exercise_id: UUID
    name: str
    sport_id: str
    text: ExerciseText | None


type ImageMediaType = Literal["image/jpeg", "image/png", "image/webp"]


@dataclass(frozen=True, slots=True)
class AmbiguousExerciseName:
    """The name matches catalogue exercises in more than one sport, so none is picked."""

    sport_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """The picture's bytes as the provider returned them, never re-encoded."""

    data: bytes
    media_type: ImageMediaType


@dataclass(frozen=True, slots=True)
class ImageGenerationFailed:
    """The image call ended without a picture; its `llm_calls` row is written either way."""

    reason: Literal["timeout", "error"]


@dataclass(frozen=True, slots=True)
class BriefFailed:
    """The brief call ended without a brief; its `llm_calls` row is written either way."""

    reason: Literal["timeout", "error"]


@dataclass(frozen=True, slots=True)
class PictureSpendLimits:
    """`IMAGE_GENERATION_SPEND_CAP_USD` for one run, and the picture's price from `Settings`,
    which the run checks against the cap before the image call (ADR-0031)."""

    cap_usd: Decimal
    price_usd: Decimal


# A result's `spent_usd` is None when one of the run's calls has no billed cost, so the run's
# spend is unknown rather than the sum of the costs that are known.


@dataclass(frozen=True, slots=True)
class PictureMade:
    exercise_id: UUID
    brief: PictureBrief
    image: GeneratedImage
    spent_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class PictureSkipped:
    """Nothing to draw from: no such catalogue exercise, or no text that says how it's done."""

    reason: Literal["no_such_exercise", "no_description"]
    spent_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class PictureRefused:
    """The email isn't an active admin's (ADR-0031), or the name matches catalogue exercises in
    more than one sport: no call is made, and no row written."""

    reason: Literal["not_admin", "ambiguous_name"]


@dataclass(frozen=True, slots=True)
class SpendCapReached:
    """The run stopped before the image call: the picture's price would take `spent_usd` over
    `cap_usd`, or a call's cost is unknown (`spent_usd` is None), so the cap can't be checked."""

    spent_usd: Decimal | None
    price_usd: Decimal
    cap_usd: Decimal


@dataclass(frozen=True, slots=True)
class PictureFailed:
    step: Literal["brief", "image"]
    reason: Literal["timeout", "error"]
    spent_usd: Decimal | None


type PictureResult = PictureMade | PictureSkipped | PictureRefused | SpendCapReached | PictureFailed


# What a stored picture's asset says about itself (ADR-0031 *Fallback media source*, #535).
GENERATED_PICTURE_SOURCE = "ai-generated"
GENERATED_PICTURE_LICENSE = (
    "Google won't claim ownership over generated content (Gemini API additional terms, "
    "https://aistudio.google.com/docs/terms)"
)
_PICTURE_EXTENSIONS: dict[ImageMediaType, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


@dataclass(frozen=True, slots=True)
class StoredPicture:
    """The picture is in the media bucket, as one asset with one `suggested` link."""

    asset_id: UUID
    exercise_id: UUID
    spent_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class PictureStoreFailed:
    """Nothing was left behind: no object, no asset, no link. `already_stored` is a picture with
    the same bytes already in the catalogue, which is kept as it is."""

    reason: Literal["storage_error", "database_error", "already_stored"]
    spent_usd: Decimal | None


def picture_storage_key(exercise_id: UUID, image: GeneratedImage) -> str:
    """`exercises/<exercise_id>/<sha256>.<ext>`: the same bytes always land on the same key."""
    digest = hashlib.sha256(image.data).hexdigest()
    return f"exercises/{exercise_id}/{digest}.{_PICTURE_EXTENSIONS[image.media_type]}"


def generated_picture_asset(
    *, exercise_id: UUID, image: GeneratedImage, model: str, generated_on: date
) -> MediaAsset:
    """The asset row for a generated picture: `licensed`, `stored`, `image`, no embed URL, with
    the attribution ADR-0031 prescribes and the model's slug in `source_ref`."""
    return MediaAsset(
        kind=MediaKind.IMAGE,
        rights=MediaRights.LICENSED,
        delivery=MediaDelivery.STORED,
        storage_key=picture_storage_key(exercise_id, image),
        source=GENERATED_PICTURE_SOURCE,
        source_ref=model,
        license=GENERATED_PICTURE_LICENSE,
        attribution=(
            "AI generated with Gemini 3.1 Flash Lite Image (Google) via OpenRouter · "
            f"{generated_on.isoformat()}"
        ),
        sha256=hashlib.sha256(image.data).hexdigest(),
    )
