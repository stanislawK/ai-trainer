"""The History page's read model and the athlete's edit and delete of a saved session (PRD 0011
F3, B4, B38, tickets #515, #516 and #518): the athlete's saved gym sessions, newest first, with
the time of day in their timezone (ADR-0014), an edit of one session's exercises and sets
validated as the draft edit is, and a delete of one session."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import JsonValue, ValidationError

from ai_trainer.application.ports.gym_history import (
    GymSessionReaderPort,
    SessionDeleterPort,
    SessionEditorPort,
)
from ai_trainer.domain.history import (
    HistorySession,
    SessionChangedError,
    SessionEditError,
    SessionNotEditableError,
    StoredGymSession,
)
from ai_trainer.domain.sports.gym.edit import apply_gym_edit
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID
from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.training import SessionNotFoundError


def _local(stored: StoredGymSession, timezone: ZoneInfo) -> HistorySession:
    first = stored.span.first_logged_at.astimezone(timezone)
    return HistorySession(
        id=stored.span.id,
        day=stored.span.occurred_on,
        time=first.time().replace(microsecond=0),
        updated_at=stored.updated_at,
        exercises=stored.exercises,
        labels=stored.labels,
    )


def _zone(name: str) -> ZoneInfo:
    """The athlete's zone, UTC when the stored name is unknown (ADR-0014)."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


class GymSessionHistory:
    def __init__(self, reader: GymSessionReaderPort) -> None:
        self._reader = reader

    async def list(self, user_id: UUID, timezone: str) -> Sequence[HistorySession]:
        """The user's sessions, the latest day first, and within a day the latest start."""
        stored = await self._reader.list_sessions(user_id)
        ordered = sorted(
            stored,
            key=lambda session: (session.span.occurred_on, session.span.first_logged_at),
            reverse=True,
        )
        zone = _zone(timezone)
        return [_local(session, zone) for session in ordered]

    async def get(self, user_id: UUID, session_id: UUID, timezone: str) -> HistorySession | None:
        """One of the user's sessions, or `None` for an unknown one or another user's."""
        stored = await self._reader.get_session(user_id, session_id)
        return None if stored is None else _local(stored, _zone(timezone))


def _without_names(changes: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The edit without any exercise's name: History never renames or remaps an exercise (B37
    stays with the chat's exercise card), so each entry keeps its exercise (ticket #516)."""
    rows = changes.get("activities")
    if not isinstance(rows, list):
        return dict(changes)
    return {
        **changes,
        "activities": [
            {key: value for key, value in row.items() if key != "name"}
            if isinstance(row, dict)
            else row
            for row in rows
        ],
    }


def _fields(error: ValidationError) -> frozenset[str]:
    """Each rejected field as the edit form names it, e.g. `activities.0.sets.1.reps`."""
    return frozenset(".".join(str(part) for part in detail["loc"]) for detail in error.errors())


def _version(token: str | None, session_id: UUID) -> datetime:
    """The session's `updated_at` as its edit form was rendered; a missing, unreadable or
    naive token reads as a change made meanwhile (ticket #516)."""
    try:
        version = datetime.fromisoformat(token or "")
    except ValueError:
        raise SessionChangedError(session_id) from None
    if version.tzinfo is None:
        raise SessionChangedError(session_id)
    return version


class GymSessionEdit:
    """The athlete's edit of one saved gym session (B4, ticket #516)."""

    def __init__(self, reader: GymSessionReaderPort, editor: SessionEditorPort) -> None:
        self._reader = reader
        self._editor = editor

    async def save(
        self,
        user_id: UUID,
        session_id: UUID,
        changes: Mapping[str, JsonValue],
        *,
        version: str | None,
    ) -> None:
        """Applies the athlete's form to the session's stored exercises as the draft edit does
        (B38: a field left out stays, a blank one is cleared, never 0) and saves the result.
        `version` is the session's `updated_at` the form was rendered with. Raises
        `SessionNotFoundError` for an unknown session or another user's,
        `SessionNotEditableError` when it has no exercises or its rows no longer read,
        `SessionEditError` naming every rejected field, and `SessionChangedError` when the
        session changed since the form was rendered; none of the last three writes anything."""
        stored = await self._reader.get_session(user_id, session_id)
        if stored is None:
            raise SessionNotFoundError(session_id)
        if not stored.exercises:
            raise SessionNotEditableError(session_id)
        try:
            current = GymPayload.model_validate(
                {"activities": [exercise.model_dump(mode="json") for exercise in stored.exercises]}
            )
        except ValidationError:
            raise SessionNotEditableError(session_id) from None
        try:
            edited = apply_gym_edit(current.model_dump(mode="json"), _without_names(changes))
        except ValidationError as error:
            raise SessionEditError(_fields(error)) from None
        await self._editor.replace_activities(
            user_id,
            session_id,
            sport_id=GYM_SPORT_ID,
            payload=edited.model_dump(mode="json"),
            expected_updated_at=_version(version, session_id),
        )


class GymSessionDelete:
    """The athlete's delete of one saved session from History (B4, ticket #518)."""

    def __init__(self, deleter: SessionDeleterPort) -> None:
        self._deleter = deleter

    async def delete(self, user_id: UUID, session_id: UUID) -> None:
        """Deletes the session for good, after the athlete confirmed it in History. Raises
        `SessionNotFoundError` for an unknown session, another user's, or one already deleted
        (another tab's delete), and then deletes nothing."""
        await self._deleter.delete(user_id, session_id)
