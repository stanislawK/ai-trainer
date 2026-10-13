"""What `gym.extract` returns (ADR-0008, ADR-0014): an absolute date, how it was found, and
each exercise with its sets, reps, load exactly as typed and RIR. The model copies the numbers
and text and picks the unit; code, not the model, converts to kg."""

from pydantic import BaseModel, Field

from ai_trainer.domain.sports.base import SessionExtraction
from ai_trainer.domain.sports.gym.normalize import normalize_load
from ai_trainer.domain.sports.gym.payload import (
    MAX_RIR,
    Exercise,
    ExerciseName,
    GymPayload,
    LoadUnit,
)


class ExtractedLoad(BaseModel):
    original: str = Field(min_length=1)
    value: float = Field(gt=0)
    # None when the message gives no unit ("@ 60"); it is read as kg, owner-chosen at #159's
    # plan gate, and the athlete can change it before confirming.
    unit: LoadUnit | None = None


class ExtractedExercise(BaseModel):
    name: ExerciseName
    sets: int | None = Field(default=None, ge=1)
    reps: int | None = Field(default=None, ge=1)
    load: ExtractedLoad | None = None
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)


class GymExtraction(SessionExtraction):
    activities: list[ExtractedExercise] = Field(min_length=1)

    def to_payload(self) -> GymPayload:
        return GymPayload(
            activities=[
                Exercise(
                    name=exercise.name,
                    sets=exercise.sets,
                    reps=exercise.reps,
                    load=(
                        normalize_load(
                            exercise.load.original, exercise.load.value, exercise.load.unit or "kg"
                        )
                        if exercise.load is not None
                        else None
                    ),
                    rir=exercise.rir,
                )
                for exercise in self.activities
            ]
        )
