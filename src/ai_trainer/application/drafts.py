"""Answering a session draft (PRD-0004 F2, ADR-0022). Only the athlete's confirm writes a
training session or adds to one (ADR-0022 invariant 3, B16); only the athlete's undo takes a
log back (B4). The
athlete's edit changes only a pending draft (ticket #158)."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, JsonValue, ValidationError

from ai_trainer.application.feedback import FeedbackRecorder, changed_fields
from ai_trainer.application.ports.clarifications import ClarificationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.draft_settling import DraftSettlerPort
from ai_trainer.application.ports.drafts import SessionDraftRepositoryPort
from ai_trainer.domain.clarification import ClarificationResolvedError
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftEditError,
    DraftNotFoundError,
    SessionDraft,
    StoredDraft,
)

# The envelope fields the edit form carries (F2); the sport's own fields are its plugin's.
_ENVELOPE_FIELDS = ("occurred_on", "duration_min", "rpe", "feeling")


@dataclass(frozen=True, slots=True)
class CardQuestions:
    """What an edit needs to drop a question about a name it took off the card (ticket #397)."""

    clarifications: ClarificationRepositoryPort
    settlers: Mapping[str, DraftSettlerPort]
    clock: ClockPort


async def confirm_draft(
    user_id: UUID, draft_id: UUID, *, drafts: SessionDraftRepositoryPort, clock: ClockPort
) -> StoredDraft:
    """Saves the athlete's own draft as a training session, now. Confirming it again returns the
    same session; another user's draft is not found, and a discarded one is refused."""
    return await drafts.confirm(user_id, draft_id, confirmed_at=clock.now())


async def discard_draft(
    user_id: UUID,
    draft_id: UUID,
    *,
    drafts: SessionDraftRepositoryPort,
    clock: ClockPort,
    feedback: FeedbackRecorder | None = None,
) -> StoredDraft:
    """Throws away the athlete's own draft, now, without saving a session. Discarding it again
    returns the same draft; another user's draft is not found, and a confirmed one is refused.
    The first discard is one `draft_discarded` feedback event (ADR-0034)."""
    before = await drafts.get(user_id, draft_id)
    discarded = await drafts.discard(user_id, draft_id, discarded_at=clock.now())
    if feedback is not None and before.status == "pending":
        await feedback.record_for_draft(user_id, FeedbackKind.DRAFT_DISCARDED, before)
    return discarded


async def undo_log(
    user_id: UUID,
    draft_id: UUID,
    *,
    drafts: SessionDraftRepositoryPort,
    clock: ClockPort,
    feedback: FeedbackRecorder | None = None,
) -> StoredDraft:
    """Takes the athlete's own confirmed draft's log back from its session, now: its source and
    the activities it brought go, and the session too when it was its last log (B4, B16). The
    draft stays confirmed, without a session. Another user's draft is not found, and one with
    no session, never saved or already undone, has nothing to undo. An undo is one
    `session_undone` feedback event (ADR-0034)."""
    undone = await drafts.undo(user_id, draft_id, undone_at=clock.now())
    if feedback is not None:
        await feedback.record_for_draft(user_id, FeedbackKind.SESSION_UNDONE, undone)
    return undone


async def find_draft(
    user_id: UUID, draft_id: UUID, *, drafts: SessionDraftRepositoryPort
) -> StoredDraft:
    """The athlete's own draft, whatever its status; another user's draft is not found."""
    return await drafts.get(user_id, draft_id)


async def draft_to_edit(
    user_id: UUID, draft_id: UUID, *, drafts: SessionDraftRepositoryPort
) -> StoredDraft:
    """The athlete's own draft, when it is still pending and so open to an edit. A confirmed
    or discarded one is refused; another user's draft is not found."""
    return _pending(await drafts.get(user_id, draft_id))


async def edit_draft(
    user_id: UUID,
    draft_id: UUID,
    changes: Mapping[str, JsonValue],
    *,
    drafts: SessionDraftRepositoryPort,
    sports: SportRegistry,
    feedback: FeedbackRecorder | None = None,
    questions: CardQuestions | None = None,
) -> StoredDraft:
    """Replaces the athlete's pending draft with their edit (F2): the date, duration, RPE and
    feeling from `changes`, validated by `SessionDraft`, and the sport's fields through its
    plugin's edit, validated by its payload model. A blank optional field clears it. A changed
    date is now one the athlete stated (ADR-0014). Raises `DraftEditError` naming every
    rejected field, and nothing changes; a confirmed or discarded draft is refused, and
    another user's draft, or one whose sport can't be edited, is not found. An edit that
    changed something is one `draft_edited` feedback event naming the changed fields, never
    their values (ADR-0034)."""
    stored = _pending(await drafts.get(user_id, draft_id))
    try:
        spec = sports.get(stored.sport_id).extraction
    except UnknownSportError:
        spec = None
    if spec is None:
        raise DraftNotFoundError(draft_id)
    invalid: set[str] = set()
    payload: BaseModel | None = None
    try:
        payload = spec.apply_edit(stored.draft.payload, changes)
    except ValidationError as error:
        invalid |= _fields(error)
    envelope = {
        name: _blank_is_none(changes.get(name)) for name in _ENVELOPE_FIELDS if name in changes
    }
    edited_payload = stored.draft.payload if payload is None else payload.model_dump(mode="json")
    draft: SessionDraft | None = None
    try:
        draft = SessionDraft.model_validate(
            {**stored.draft.model_dump(mode="json"), **envelope, "payload": edited_payload}
        )
    except ValidationError as error:
        invalid |= _fields(error)
    if invalid or draft is None:
        raise DraftEditError(frozenset(invalid))
    if draft.occurred_on != stored.draft.occurred_on:
        draft = draft.model_copy(update={"date_source": "stated"})
    saved = await drafts.update(user_id, draft_id, draft, payload_version=spec.payload_version)
    # Compared in the payload's current shape, so an upcast from an older version isn't an edit.
    before = spec.payload_type.model_validate(stored.draft.payload).model_dump(mode="json")
    fields = changed_fields(stored.draft.model_copy(update={"payload": before}), saved.draft)
    if feedback is not None and fields:
        await feedback.record_for_draft(user_id, FeedbackKind.DRAFT_EDITED, saved, fields=fields)
    if questions is not None:
        await _drop_stale_question(user_id, stored, saved, questions)
    return saved


async def _drop_stale_question(
    user_id: UUID, before: StoredDraft, draft: StoredDraft, questions: CardQuestions
) -> None:
    """An edit asks about no name it typed (ticket #397), but a question about a name it took off
    the card would hold the Confirm back over nothing, so it is closed, and the card moves on to
    the next name it was already waiting to ask about. A tap that got there first closed it
    already, and asked its own next question."""
    settler = questions.settlers.get(draft.sport_id)
    if settler is None:
        return
    asked = [
        question
        for question in await questions.clarifications.list_shown(user_id, draft.conversation_id)
        if question.draft_id == draft.id
    ]
    stale = [
        question
        for question in asked
        if question.status == "open"
        and question.clarification.subject is not None
        and not settler.mentions(draft.draft.payload, question.clarification.subject)
    ]
    for question in stale:
        try:
            await questions.clarifications.resolve(
                user_id,
                question.id,
                "abandoned",
                option_ids=None,
                resolved_at=questions.clock.now(),
            )
        except ClarificationResolvedError:
            return
    if not stale:
        return
    # Names typed in this edit are typed-only (ADR-0015 #397 amendment), and so are the ones the
    # athlete passed over with "Let me type instead".
    typed_now = settler.names(draft.draft.payload) - settler.names(before.draft.payload)
    passed_over = {
        question.clarification.subject
        for question in asked
        if question.answer_option_ids == () and question.clarification.subject is not None
    }
    following = await settler.next_question(
        user_id, draft.draft.payload, settled={*typed_now, *passed_over}
    )
    if following is not None:
        await questions.clarifications.add(
            user_id,
            draft.conversation_id,
            draft.message_id,
            following.card_purpose,
            following,
            created_at=questions.clock.now(),
            draft_id=draft.id,
        )


def _pending(draft: StoredDraft) -> StoredDraft:
    if draft.status == "confirmed":
        raise DraftConfirmedError(draft.id)
    if draft.status == "discarded":
        raise DraftDiscardedError(draft.id)
    return draft


def _blank_is_none(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return value.strip() or None
    return value


def _fields(error: ValidationError) -> set[str]:
    """Each rejected field as the edit form names it, e.g. `activities.0.count`."""
    return {".".join(str(part) for part in detail["loc"]) for detail in error.errors()}
