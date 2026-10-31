"""Recording feedback events (ADR-0034, PRD-0010 G6). A row says that an athlete corrected or
declined what the app made, by kind, template and field names. It never holds message text or a
typed value, and it is best effort: a failed write is logged by its type and never fails the
athlete's own action."""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.feedback import FeedbackEventsPort
from ai_trainer.domain.feedback import FeedbackCount, FeedbackKind, FeedbackSignal, NewFeedbackEvent
from ai_trainer.domain.training import SessionDraft, StoredDraft

logger = logging.getLogger(__name__)

# The envelope fields the edit form carries (F2); the sport's own fields are its payload's.
_ENVELOPE_FIELDS = ("occurred_on", "duration_min", "rpe", "feeling")


class FeedbackRecorder:
    """The events port with the clock's time, so an action records with one call."""

    def __init__(self, events: FeedbackEventsPort, *, clock: ClockPort) -> None:
        self._events = events
        self._clock = clock

    async def record(
        self,
        user_id: UUID,
        kind: FeedbackKind,
        *,
        template_id: str | None = None,
        template_version: int | None = None,
        fields: Sequence[str] = (),
    ) -> None:
        try:
            await self._events.record(
                NewFeedbackEvent(
                    user_id=user_id,
                    kind=kind,
                    template_id=template_id,
                    template_version=template_version,
                    fields=sorted(set(fields)),
                    created_at=self._clock.now(),
                )
            )
        except Exception as error:  # feedback must never fail the athlete's own action
            logger.warning("feedback event not recorded: %s", type(error).__name__)

    async def record_for_draft(
        self, user_id: UUID, kind: FeedbackKind, draft: StoredDraft, *, fields: Sequence[str] = ()
    ) -> None:
        await self.record(
            user_id,
            kind,
            template_id=draft.template_id,
            template_version=draft.template_version,
            fields=fields,
        )


def changed_fields(before: SessionDraft, after: SessionDraft) -> list[str]:
    """The names of the fields an edit changed, never what they changed to: the envelope's
    (`rpe`) and the payload's, through its lists of rows (`reps` of any set of any exercise,
    or `sets` when their number changed)."""
    names = {name for name in _ENVELOPE_FIELDS if getattr(before, name) != getattr(after, name)}
    return sorted(names | _payload_fields(before.payload, after.payload))


def _payload_fields(before: Mapping[str, JsonValue], after: Mapping[str, JsonValue]) -> set[str]:
    names: set[str] = set()
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if old != new:
            names |= _row_fields(old, new) or {key}
    return names


def _row_fields(old: JsonValue, new: JsonValue) -> set[str]:
    """For two equally long lists of objects, as a gym draft's exercises and their sets, the
    keys that differ in any row; empty for anything else, so the list names itself."""
    if not (isinstance(old, list) and isinstance(new, list)) or len(old) != len(new):
        return set()
    names: set[str] = set()
    for row_old, row_new in zip(old, new, strict=True):
        if not (isinstance(row_old, dict) and isinstance(row_new, dict)):
            return set()
        names |= _payload_fields(row_old, row_new)
    return names


def count_signals(signals: Sequence[FeedbackSignal]) -> list[FeedbackCount]:
    """Counts per kind, template and field, largest first; an event naming no field counts
    once under `field=None`."""
    counts: dict[tuple[FeedbackKind, str | None, str | None], int] = {}
    for signal in signals:
        for field in signal.fields or (None,):
            key = (signal.kind, signal.template_id, field)
            counts[key] = counts.get(key, 0) + 1
    return [
        FeedbackCount(kind=kind, template_id=template, field=field, count=count)
        for (kind, template, field), count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1] or "", item[0][2] or ""),
        )
    ]
