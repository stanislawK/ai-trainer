"""A sport's extraction and the save of a reply's parts: the pending draft the athlete confirms
later (PRD-0004 B1, F2, ADR-0022), offered as an addition to their latest session in its sport
when it came close after it (B16, ADR-0014) or asking whether it is that session when it came
later that day (ticket #166), or the question of its readings when the extraction can't read
the log safely (B17, ticket #164)."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from ai_trainer.application.ports.conversation import ReplyContext, TemplateRef
from ai_trainer.application.reply.shared import (
    LOG_KIND,
    TICK,
    Answer,
    ReplyDone,
    ReplyFailed,
    ReplyServices,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    NEW_SESSION_OPTION,
    READING_PURPOSE,
    SAME_SESSION_ADD_COPY_KEY,
    SAME_SESSION_NEW_COPY_KEY,
    ChoiceOption,
    Clarification,
    StoredClarification,
)
from ai_trainer.domain.sports.base import ExtractionQuestion
from ai_trainer.domain.training import (
    Continuity,
    SessionDraft,
    SessionSpan,
    StoredDraft,
    continuity,
)


async def _recent(user_id: UUID, sport_id: str, services: ReplyServices) -> tuple[str, ...]:
    """The sport's context from the athlete's latest sessions in it, as its plugin reads it
    (ticket #164); none for a plugin that reads none."""
    spec = services.sports.get(sport_id).extraction
    if spec is None or spec.recent is None:
        return ()
    return spec.recent(
        await services.sessions.recent_payloads(user_id, sport_id, services.recent_sessions)
    )


async def extract(
    context: ReplyContext,
    sport_id: str,
    services: ReplyServices,
    *,
    answer: ChoiceOption | None = None,
    may_ask: bool = True,
) -> Answer | ReplyFailed:
    """The sport's extraction, as a draft in the athlete's timezone (ADR-0014, ADR-0022) under
    a fixed intro line, or as the question it asks instead, unless it `may_ask` not or carries
    the option the athlete picked on it (`answer`, ticket #164). A failed call ends the reply;
    nothing of it is kept."""
    answers = () if answer is None else (answer.id,)
    extraction = await services.conversation.extract(
        context,
        sport_id,
        recent=await _recent(context.user_id, sport_id, services),
        answers=answers,
        may_ask=may_ask,
    )
    if extraction.output is None:
        return ReplyFailed(outcome=extraction.outcome)
    if isinstance(extraction.output, ExtractionQuestion):
        # The template can only draft when it may not ask or carries the athlete's answer.
        assert may_ask
        assert not answers
        return _reading_question(sport_id, extraction.output, services)
    spec = services.sports.get(sport_id).extraction
    assert spec is not None  # the plan only sends sports that name an extraction
    # The athlete's pick holds whatever the model made of it (ADR-0015 invariant 4).
    output = extraction.output if answer is None else extraction.output.answered(answer)
    draft = SessionDraft(
        occurred_on=output.occurred_on,
        date_source=output.date_source,
        timezone=context.timezone,
        payload=output.to_payload().model_dump(mode="json"),
    )
    intro = services.conversation.draft_ready()
    return Answer(
        intro.text,
        extraction.template,
        (sport_id,),
        draft=(sport_id, draft, spec.payload_version),
    )


def _reading_question(
    sport_id: str, question: ExtractionQuestion, services: ReplyServices
) -> Answer:
    """The question's fixed line, and the question with one option per reading, likeliest
    first, cut to `max_clarification_options` (ADR-0015). Nothing but the question is written
    before the tap (invariant 3)."""
    fixed = services.conversation.reading_question()
    return Answer(
        fixed.text,
        fixed.template,
        (sport_id,),
        question=Clarification(
            question=fixed.text,
            options=question.options()[: services.max_clarification_options],
            pending=LOG_KIND,
            sport=sport_id,
        ),
    )


def draft_failed(sport_id: str, services: ReplyServices) -> Answer:
    """The line saved in place of the sport's draft when its extraction failed beside a part
    that came through, so the athlete can retry that sport alone (ticket #161)."""
    fixed = services.conversation.draft_failed()
    return Answer(fixed.text, fixed.template, (sport_id,))


async def save(
    user_id: UUID,
    message: ChatMessage,
    answers: Sequence[Answer],
    services: ReplyServices,
    *,
    after: datetime | None = None,
) -> ReplyDone:
    """A reply belongs to the message it follows (ADR-0008), so its parts are stamped right
    after that message, in its conversation, not when they were saved: a later message never
    splits them off. Each saved part moves the conversation's last activity to now (ADR-0021).
    A part's draft is stored pending, made from the athlete's message; no session is written
    (ADR-0022 invariant 3). It extends the athlete's latest session in its sport when the
    message was sent within the merge window of that session's latest log (B16), and asks
    whether it is that session when it came later that day (ticket #166). `after` stamps the
    parts after a reply already saved to the message, as a draft after its question."""
    saved: list[ChatMessage] = []
    drafts: list[StoredDraft] = []
    questions: list[StoredClarification] = []
    stamp: datetime = after or message.created_at
    for answer in answers:
        stamp += TICK
        saved.append(
            await services.chat.add(
                user_id,
                message.conversation_id,
                ChatRole.ASSISTANT,
                answer.text,
                stamp,
                sports=answer.sports,
                template_id=answer.template.id,
                template_version=answer.template.version,
            )
        )
        if answer.draft is not None:
            sport_id, draft, payload_version = answer.draft
            stored, question = await store_draft(
                user_id,
                message,
                sport_id=sport_id,
                draft=draft,
                payload_version=payload_version,
                template=answer.template,
                services=services,
            )
            drafts.append(stored)
            if question is not None:
                questions.append(question)
        if answer.question is not None:
            questions.append(
                await services.clarifications.add(
                    user_id,
                    message.conversation_id,
                    message.id,
                    READING_PURPOSE,
                    answer.question,
                    created_at=services.clock.now(),
                )
            )
        await services.conversations.touch(user_id, message.conversation_id, services.clock.now())
    return ReplyDone(messages=saved, drafts=tuple(drafts), clarifications=tuple(questions))


async def session_continuity(
    user_id: UUID,
    message: ChatMessage,
    sport_id: str,
    draft: SessionDraft,
    services: ReplyServices,
) -> tuple[Continuity, SessionSpan | None]:
    """What the log is to the athlete's latest session in the sport (ADR-0014): an addition to
    it when sent within the merge window of its latest log, on a day the draft can belong to
    (B16); a question when sent later on its day (ticket #166); else a session of its own."""
    latest = await services.sessions.latest_in_sport(user_id, sport_id)
    if latest is None:
        return "new", None
    return continuity(draft, message.created_at, latest, services.merge_window), latest


async def store_draft(
    user_id: UUID,
    message: ChatMessage,
    *,
    sport_id: str,
    draft: SessionDraft,
    payload_version: int,
    template: TemplateRef,
    services: ReplyServices,
) -> tuple[StoredDraft, StoredClarification | None]:
    """The pending draft, extending the session it joins, and the same-session question on
    its card when it came later that day, stored with it in one transaction; the question
    extends nothing (ADR-0014 invariant 5). It records the extraction `template` it came from
    (ADR-0034)."""
    kind, latest = await session_continuity(user_id, message, sport_id, draft, services)
    settled, asks = await settle_draft(
        user_id, sport_id, draft, kind=kind, latest=latest, services=services
    )
    stored = await services.drafts.add(
        user_id,
        message.conversation_id,
        message.id,
        sport_id,
        settled,
        payload_version=payload_version,
        created_at=services.clock.now(),
        extends_session_id=latest.id if kind == "join" and latest is not None else None,
        asks=asks,
        template_id=template.id,
        template_version=template.version,
    )
    return stored, await _question_on(user_id, stored, services)


async def settle_draft(
    user_id: UUID,
    sport_id: str,
    draft: SessionDraft,
    *,
    kind: Continuity,
    latest: SessionSpan | None,
    services: ReplyServices,
) -> tuple[SessionDraft, Clarification | None]:
    """The draft with what its sport's settler knows filled in (ticket #397), and the first
    question its card asks. One at a time (ADR-0015 invariant 1): the same-session one first,
    and a name's when its answer comes, so no shortlist is read for a name then."""
    settler = services.settlers.get(sport_id)
    payload = draft.payload if settler is None else await settler.settle(user_id, draft.payload)
    settled = draft.model_copy(update={"payload": payload})
    if kind == "ask" and latest is not None:
        return settled, same_session_question(draft, latest, services)
    if settler is None:
        return settled, None
    return settled, await settler.next_question(user_id, payload)


def same_session_question(
    draft: SessionDraft, session: SessionSpan, services: ReplyServices
) -> Clarification:
    """The question on the draft's card (ticket #166): "Add to the session at HH:MM", the
    session's first log in the draft's own timezone, or "New session". The card's Discard is
    the way out, so there is no "let me type instead"."""
    question = services.conversation.same_session_question()
    started = session.first_logged_at.astimezone(ZoneInfo(draft.timezone))
    return Clarification(
        question=question.text,
        options=(
            ChoiceOption(
                id=str(session.id),
                copy_key=SAME_SESSION_ADD_COPY_KEY,
                params={"time": f"{started:%H:%M}"},
            ),
            ChoiceOption(id=NEW_SESSION_OPTION, copy_key=SAME_SESSION_NEW_COPY_KEY),
        ),
        allow_free_text=False,
        pending=LOG_KIND,
    )


async def _question_on(
    user_id: UUID, draft: StoredDraft, services: ReplyServices
) -> StoredClarification | None:
    """The question still open on the draft's card, if it asks one."""
    asked = await services.clarifications.list_shown(user_id, draft.conversation_id)
    return next(
        (
            question
            for question in asked
            if question.draft_id == draft.id and question.status == "open"
        ),
        None,
    )
