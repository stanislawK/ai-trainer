from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# `exercises.name` and `exercise_aliases.alias` hold at most this much, so a longer typed name
# stays typed-only (ticket #397).
MAX_EXERCISE_NAME = 200


class ExerciseMeasure(StrEnum):
    """What one set of an exercise is measured in (ADR-0032)."""

    LOAD_REPS = "load_reps"
    REPS = "reps"
    TIME = "time"
    DISTANCE = "distance"
    BAND_REPS = "band_reps"


class ExerciseText(BaseModel):
    """An imported description of an exercise in one language (ADR-0031, G5)."""

    model_config = ConfigDict(frozen=True)

    lang: str
    instructions: str
    steps: tuple[str, ...]


class CatalogueExercise(BaseModel):
    """A shared catalogue row as imported, keyed by `(source, source_ref)` (ADR-0031). It holds
    no user data."""

    model_config = ConfigDict(frozen=True)

    sport_id: str
    source: str
    source_ref: str
    name: str
    equipment: str
    body_part: str
    target: str
    secondary_muscles: tuple[str, ...]
    default_measure: ExerciseMeasure
    attribution: str
    texts: tuple[ExerciseText, ...]


class ImportedCatalogue(BaseModel):
    """Everything one import writes, validated before any of it reaches the database."""

    model_config = ConfigDict(frozen=True)

    exercises: tuple[CatalogueExercise, ...]
    skipped: int


class CatalogueWrite(BaseModel):
    """What an upsert did. A row that already matched is left unwritten and counts as unchanged."""

    model_config = ConfigDict(frozen=True)

    inserted: int
    updated: int
    unchanged: int
    texts_written: int


class CatalogueImportReport(BaseModel):
    """What one import found and wrote."""

    model_config = ConfigDict(frozen=True)

    total: int
    skipped: int
    write: CatalogueWrite


class Exercise(BaseModel):
    """An exercise as an athlete sees it: a catalogue row, or their own when `owner_user_id` is
    set (ADR-0031 invariant 3)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    sport_id: str
    owner_user_id: UUID | None
    name: str
    default_measure: ExerciseMeasure


class ExerciseCandidate(BaseModel):
    """A shortlisted exercise for a typed name (ADR-0031): what a choice card tells it apart by."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    equipment: str | None = None
    target: str | None = None


class ExerciseAlias(BaseModel):
    """A name an athlete confirmed for an exercise (ADR-0031 invariant 1)."""

    model_config = ConfigDict(frozen=True)

    exercise_id: UUID
    alias: str
    alias_norm: str


class ExerciseDatasetError(Exception):
    """The pinned dataset could not be downloaded, or isn't the shape we import."""


class ExerciseCatalogueError(Exception):
    """The committed catalogue files are invalid, or don't fit the pinned dataset."""
