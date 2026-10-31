"""The gym exercise catalogue's import rules (ADR-0031): cleaning the pinned dataset's names,
deriving a default measure, and applying the committed overrides and supplement. Pure: the files
and the download are read by an adapter."""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_trainer.domain.exercises import (
    CatalogueExercise,
    ExerciseCatalogueError,
    ExerciseMeasure,
    ExerciseText,
    ImportedCatalogue,
)

SPORT_ID = "gym"
DATASET_SOURCE = "exercises-dataset"
EXTRA_SOURCE = "ai-trainer"
# The source holds ten languages. G5 stores what we can match on, English and Polish.
IMPORTED_LANGS = ("en", "pl")
_DATASET_AUTHOR = "Hasan Emir Yıldırım"  # noqa: RUF001 -- the name as their license writes it
_GENDER_SUFFIX = re.compile(r"\s*\((?:male|female)\)\s*$", re.IGNORECASE)
_WORD = re.compile(r"[^\s()/-]+")


class DatasetRow(BaseModel):
    """One record of `data/exercises.json`. Its media fields and its `attribution`, a credit for
    that media, are left out on purpose: we hold no license for the media (ADR-0031)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    equipment: str
    body_part: str
    target: str
    secondary_muscles: tuple[str, ...]
    instructions: Mapping[str, str]
    instruction_steps: Mapping[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _has_every_imported_language(self) -> Self:
        for lang in IMPORTED_LANGS:
            if not self.instructions.get(lang) or not self.instruction_steps.get(lang):
                raise ValueError(f"exercise {self.id} has no {lang} text")
        return self


class CatalogueOverrides(BaseModel):
    """`data/catalogue/overrides.yaml`, keyed by the dataset's `id`. `words` holds the words plain
    title-casing gets wrong; a `rename` value is the finished name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skip: frozenset[str] = frozenset()
    rename: Mapping[str, str] = Field(default_factory=dict)
    measure: Mapping[str, ExerciseMeasure] = Field(default_factory=dict)
    words: Mapping[str, str] = Field(default_factory=dict)


class ExtraExercise(BaseModel):
    """One entry of `data/catalogue/extra.yaml`, a common exercise the dataset lacks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(min_length=1)
    name: str = Field(min_length=1)
    equipment: str
    body_part: str
    target: str
    secondary_muscles: tuple[str, ...]
    default_measure: ExerciseMeasure
    texts: tuple[ExerciseText, ...] = Field(min_length=1)


class DatasetPin(BaseModel):
    """The dataset commit an import reads and the day that pin was fetched (G10)."""

    model_config = ConfigDict(frozen=True)

    commit: str
    fetched_on: date


def clean_name(raw: str, words: Mapping[str, str]) -> str:
    """The source's lower-case name, title-cased and without a `(male)` or `(female)` suffix."""
    return _WORD.sub(
        lambda match: words.get(match[0], match[0][0].upper() + match[0][1:]),
        _GENDER_SUFFIX.sub("", raw),
    )


def derive_measure(equipment: str) -> ExerciseMeasure:
    match equipment:
        case "body weight":
            return ExerciseMeasure.REPS
        case "band" | "resistance band":
            return ExerciseMeasure.BAND_REPS
        case _:
            return ExerciseMeasure.LOAD_REPS


def build_catalogue(
    rows: Sequence[DatasetRow],
    *,
    overrides: CatalogueOverrides,
    extras: Sequence[ExtraExercise],
    pin: DatasetPin,
) -> ImportedCatalogue:
    """Everything one import writes. Raises `ExerciseCatalogueError` when an override names a row
    the dataset doesn't hold, or two rows would share a name."""
    _require_known_rows(rows, overrides)
    kept = [row for row in rows if row.id not in overrides.skip]
    exercises = (
        *(_from_row(row, overrides, _attribution(pin)) for row in kept),
        *(_from_extra(extra) for extra in extras),
    )
    _require_unique_names(exercises)
    return ImportedCatalogue(exercises=exercises, skipped=len(rows) - len(kept))


def _attribution(pin: DatasetPin) -> str:
    return (
        f"Exercise data: {_DATASET_AUTHOR}, {DATASET_SOURCE} (MIT), commit {pin.commit[:7]}, "
        f"fetched {pin.fetched_on.isoformat()}"
    )


def _from_row(
    row: DatasetRow, overrides: CatalogueOverrides, attribution: str
) -> CatalogueExercise:
    return CatalogueExercise(
        sport_id=SPORT_ID,
        source=DATASET_SOURCE,
        source_ref=row.id,
        name=overrides.rename.get(row.id) or clean_name(row.name, overrides.words),
        equipment=row.equipment,
        body_part=row.body_part,
        target=row.target,
        secondary_muscles=row.secondary_muscles,
        default_measure=overrides.measure.get(row.id) or derive_measure(row.equipment),
        attribution=attribution,
        texts=tuple(
            ExerciseText(
                lang=lang, instructions=row.instructions[lang], steps=row.instruction_steps[lang]
            )
            for lang in IMPORTED_LANGS
        ),
    )


def _from_extra(extra: ExtraExercise) -> CatalogueExercise:
    return CatalogueExercise(
        sport_id=SPORT_ID,
        source=EXTRA_SOURCE,
        source_ref=extra.ref,
        name=extra.name,
        equipment=extra.equipment,
        body_part=extra.body_part,
        target=extra.target,
        secondary_muscles=extra.secondary_muscles,
        default_measure=extra.default_measure,
        attribution=EXTRA_SOURCE,
        texts=extra.texts,
    )


def _require_known_rows(rows: Sequence[DatasetRow], overrides: CatalogueOverrides) -> None:
    named = overrides.skip | set(overrides.rename) | set(overrides.measure)
    unknown = named - {row.id for row in rows}
    if unknown:
        raise ExerciseCatalogueError(f"overrides name unknown rows: {sorted(unknown)}")


def _require_unique_names(exercises: Sequence[CatalogueExercise]) -> None:
    counts = Counter(" ".join(exercise.name.casefold().split()) for exercise in exercises)
    repeated = sorted(name for name, count in counts.items() if count > 1)
    if repeated:
        raise ExerciseCatalogueError(f"names shared by more than one row: {repeated}")
