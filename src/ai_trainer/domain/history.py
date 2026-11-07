"""A saved gym session as the History page reads it (PRD 0011 F3, B4, ticket #515): its day and
local time, and its exercises in the shape of the gym payload, so the page formats them like the
chat's logged card does."""

from collections.abc import Mapping
from datetime import date, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.exercises import ExerciseLabel
from ai_trainer.domain.sports.gym.payload import Exercise
from ai_trainer.domain.training import SessionSpan


class StoredGymSession(BaseModel):
    """What a reader returns for one session: the span of its logs (in UTC) and its entries in
    the order logged. `labels` names the entries settled to a catalogue or custom exercise, by
    the name as typed."""

    model_config = ConfigDict(frozen=True)

    span: SessionSpan
    exercises: tuple[Exercise, ...]
    labels: Mapping[str, ExerciseLabel]


class HistorySession(BaseModel):
    """A stored session for the athlete: `day` is the day it happened on, `time` is when its
    first log was sent, in the athlete's timezone (ADR-0014)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    day: date
    time: time
    exercises: tuple[Exercise, ...]
    labels: Mapping[str, ExerciseLabel]
