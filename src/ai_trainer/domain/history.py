"""A saved gym session as the History page reads it (PRD 0011 F3, B4, ticket #515): its day and
local time, and its exercises in the shape of the gym payload, so the page formats them like the
chat's logged card does."""

from collections.abc import Mapping
from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.exercises import ExerciseLabel
from ai_trainer.domain.sports.gym.payload import Exercise
from ai_trainer.domain.training import SessionSpan


class StoredGymSession(BaseModel):
    """What a reader returns for one session: the span of its logs (in UTC) and its entries in
    the order logged. `labels` names the entries settled to a catalogue or custom exercise, by
    the name as typed. `updated_at` is the session row's, which the edit form posts back to
    detect a change made meanwhile (ticket #516)."""

    model_config = ConfigDict(frozen=True)

    span: SessionSpan
    updated_at: datetime
    exercises: tuple[Exercise, ...]
    labels: Mapping[str, ExerciseLabel]


class HistorySession(BaseModel):
    """A stored session for the athlete: `day` is the day it happened on, `time` is when its
    first log was sent, in the athlete's timezone (ADR-0014). `updated_at` is the session row's
    last write, in UTC, as the edit form's version token (ticket #516)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    day: date
    time: time
    updated_at: datetime
    exercises: tuple[Exercise, ...]
    labels: Mapping[str, ExerciseLabel]


class SessionEditError(Exception):
    """The athlete's edit of a saved session has values the payload model rejects (B4, B38,
    ticket #516). `fields` names each one as the edit form does, e.g.
    `activities.0.sets.1.reps`; the session is left as it was."""

    def __init__(self, fields: frozenset[str]) -> None:
        super().__init__(f"invalid fields: {sorted(fields)}")
        self.fields = fields


class SessionNotEditableError(Exception):
    """The saved session's rows no longer read as a payload a later rule accepts, so it can't
    be edited as it is (ticket #516); its rows stay as they are."""


class SessionChangedError(Exception):
    """The saved session changed since its edit form was rendered, e.g. a log was added or
    undone meanwhile, or the form's version token is missing (ticket #516); nothing is
    written."""
