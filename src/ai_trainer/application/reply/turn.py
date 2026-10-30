"""One reply turn (PRD-0003 F1, B13, B14, G3): the message routed while a Jev safety screen
runs alongside (ADR-0020), its parts planned safety first, streamed in message order, and saved
only once all of it came through, so a retry after a failure always streams a fresh reply. A
message already answered replays what was saved."""

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from ai_trainer.application.ports.conversation import ReplyContext
from ai_trainer.application.ports.llm_gateway import StreamEnd
from ai_trainer.application.reply.clarification import ask_sport
from ai_trainer.application.reply.extract import draft_failed, extract, save
from ai_trainer.application.reply.route import route_message
from ai_trainer.application.reply.shared import (
    LOG_KIND,
    PART_BREAK,
    Answer,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    ReplyPhase,
    ReplyServices,
    extractable,
    find_message,
    reply_context,
)
from ai_trainer.domain.chat import ChatMessage
from ai_trainer.domain.conversation import Intent, IntentKind
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.training import StoredDraft

# Until each specialist ships, these go to `chitchat`; the persona carries G3 (ADR-0008).
_CHITCHAT_KINDS = frozenset({"chitchat", "unclear", "wellbeing_or_injury"})
_SAFETY_KIND: IntentKind = "wellbeing_or_injury"

type _Specialist = Literal["chitchat", "extract", "not_yet"]
# What a part streams: its text, then its answer once it came through, or the failure.
type _PartEvent = ReplyChunk | ReplyFailed | Answer


@dataclass(frozen=True, slots=True)
class _Part:
    specialist: _Specialist
    sports: tuple[str, ...] = ()


def with_screened_safety(intents: Sequence[Intent], *, pain: bool) -> list[Intent]:
    """The router's intents, plus the safety intent when the screen found pain the router
    missed. The screen only ever adds it, never removes or reorders an intent (ADR-0020
    invariant 5); `_plan` puts it first."""
    if pain and all(intent.kind != _SAFETY_KIND for intent in intents):
        return [*intents, Intent(kind=_SAFETY_KIND)]
    return list(intents)


def _plan(intents: Sequence[Intent], loggable: frozenset[str]) -> list[_Part]:
    """One part per specialist, safety first and then in message order (ADR-0008 invariant 4).
    Intents bound for the same specialist share one part: the chitchat call sees the whole
    message, one extraction per sport sees it too, and one "not yet" reply covers every intent
    that has no specialist yet. `loggable` are the sports whose plugin names an extraction
    template (ADR-0006)."""
    ordered = sorted(intents, key=lambda intent: intent.kind != _SAFETY_KIND)
    keys: list[tuple[_Specialist, str | None]] = []
    not_yet_sports: list[str] = []
    for intent in ordered:
        key: tuple[_Specialist, str | None]
        if intent.kind in _CHITCHAT_KINDS:
            key = ("chitchat", None)
        elif intent.kind == LOG_KIND and intent.sport in loggable:
            key = ("extract", intent.sport)
        else:
            key = ("not_yet", None)
            if intent.sport is not None and intent.sport not in not_yet_sports:
                not_yet_sports.append(intent.sport)
        if key not in keys:
            keys.append(key)
    return [
        _Part(specialist, (sport,) if sport is not None else ())
        if specialist != "not_yet"
        else _Part(specialist, tuple(not_yet_sports))
        for specialist, sport in keys
    ]


async def find_saved_reply(
    user_id: UUID, message_id: UUID, *, services: ReplyServices
) -> ReplyDone:
    """What was saved for the athlete's own message, with its pending drafts; no messages if
    its reply never finished: the answer to Stop arriving just after the last token."""
    message = await find_message(
        user_id, message_id, chat=services.chat, conversations=services.conversations
    )
    saved = await services.chat.list_replies(user_id, message)
    return await _replayed(user_id, message, saved, services)


async def start_reply(
    user_id: UUID, message_id: UUID, *, timezone: str, services: ReplyServices
) -> AsyncGenerator[ReplyEvent]:
    """Checks the message is the user's before anything streams, then returns the reply's
    events. A message already answered replays its saved reply with no model call."""
    message = await find_message(
        user_id, message_id, chat=services.chat, conversations=services.conversations
    )
    return _reply(user_id, message, timezone, services)


async def _reply(
    user_id: UUID, message: ChatMessage, timezone: str, services: ReplyServices
) -> AsyncGenerator[ReplyEvent]:
    saved = await services.chat.list_replies(user_id, message)
    if saved:
        yield await _replayed(user_id, message, saved, services)
        return
    async with aclosing(_fresh_reply(user_id, message, timezone, services)) as events:
        async for event in events:
            if isinstance(event, ReplyDone) and event.drafts:
                # From sending the log to its draft card (G14); a replayed reply is not a turn.
                elapsed = services.clock.now() - message.created_at
                services.metrics.log_turn(elapsed.total_seconds())
            yield event


async def _fresh_reply(
    user_id: UUID, message: ChatMessage, timezone: str, services: ReplyServices
) -> AsyncGenerator[ReplyEvent]:
    context = await reply_context(user_id, message, timezone, services)
    # Started before the router is awaited, so the screen adds no wait when the router is
    # slower; cancelled with the reply if the reader leaves while routing.
    screening = asyncio.ensure_future(services.safety_screen.finds_pain(user_id, message.text))
    try:
        yield ReplyPhase("routing")
        routing = await route_message(context, services)
        # Awaited even when routing failed, so the screen's call is recorded as it ended.
        pain = await screening
    finally:
        screening.cancel()
    if routing.outcome is not LlmCallOutcome.SUCCESS:
        yield ReplyFailed(outcome=routing.outcome)
        return

    if routing.sport_choice and not pain:
        # Saved in its own task, as the reply is: a reader who leaves now still finds the card.
        asking = asyncio.ensure_future(ask_sport(user_id, message, routing.sport_choice, services))
        yield await asyncio.shield(asking)
        return

    yield ReplyPhase("replying")

    answers: list[Answer] = []
    plan = _plan(with_screened_safety(routing.intents, pain=pain), extractable(services.sports))
    async with aclosing(_answer(context, plan, services)) as events:
        async for event in events:
            if isinstance(event, Answer):
                answers.append(event)
                continue
            yield event
            if isinstance(event, ReplyFailed):
                return

    # Saved in its own task: a reader who leaves now can't cut a reply off halfway through.
    saving = asyncio.ensure_future(save(user_id, message, answers, services))
    yield await asyncio.shield(saving)


async def _answer(
    context: ReplyContext, plan: Sequence[_Part], services: ReplyServices
) -> AsyncGenerator[_PartEvent]:
    """Each part of the plan streamed in its place. A `ReplyFailed` ends the reply, and the
    reader stops there: a failed chitchat's at once, and a failed extraction's only when
    nothing the models made came through; else the extraction is kept as a line its Retry can
    turn into the draft (ticket #161)."""
    # Every extraction starts now, so a log of two sports waits only as long as the slower one
    # (G14); each part is still streamed in its place.
    extractions = {
        part.sports[0]: asyncio.ensure_future(extract(context, part.sports[0], services))
        for part in plan
        if part.specialist == "extract"
    }
    failures: list[ReplyFailed] = []
    answered = came_through = False
    try:
        for part in plan:
            if answered:
                yield ReplyChunk(PART_BREAK)
            if part.specialist == "chitchat":
                async with aclosing(_chitchat(context, part, services)) as stream:
                    async for event in stream:
                        yield event
                        if isinstance(event, Answer):
                            answered = came_through = True
                continue
            answer, failure, made = await _fixed(part, extractions, services)
            if failure is not None:
                failures.append(failure)
            answered, came_through = True, came_through or made
            yield ReplyChunk(answer.text)
            yield answer
    finally:
        for extraction in extractions.values():
            extraction.cancel()
    if failures and not came_through:
        # Nothing the models made came through: the reply fails whole, and a retry streams a
        # fresh one (ADR-0008).
        yield failures[0]


async def _fixed(
    part: _Part,
    extractions: Mapping[str, asyncio.Future[Answer | ReplyFailed]],
    services: ReplyServices,
) -> tuple[Answer, ReplyFailed | None, bool]:
    """A part that is not streamed: the "not yet" line, or the sport's extraction as it ended,
    with the line saved in its place when it failed; and whether a model made it."""
    if part.specialist == "not_yet":
        fixed = services.conversation.not_yet()
        return Answer(fixed.text, fixed.template, part.sports), None, False
    answer = await extractions[part.sports[0]]
    if isinstance(answer, ReplyFailed):
        return draft_failed(part.sports[0], services), answer, False
    return answer, None, True


async def _chitchat(
    context: ReplyContext, part: _Part, services: ReplyServices
) -> AsyncGenerator[_PartEvent]:
    """The chitchat part, streamed as it comes, then its answer or its failure."""
    async with aclosing(services.conversation.stream_chitchat(context)) as stream:
        async for event in stream:
            if not isinstance(event, StreamEnd):
                yield ReplyChunk(event.text)
            elif event.result.output is None:
                yield ReplyFailed(outcome=event.result.outcome)
            else:
                template = services.conversation.chitchat_template
                yield Answer(event.result.output, template, part.sports)


async def _replayed(
    user_id: UUID, message: ChatMessage, saved: Sequence[ChatMessage], services: ReplyServices
) -> ReplyDone:
    """What was saved for the message, with the drafts and questions it left; none of either
    when its reply never finished."""
    if not saved:
        return ReplyDone(messages=saved)
    asked = await services.clarifications.list_shown(user_id, message.conversation_id)
    return ReplyDone(
        messages=saved,
        drafts=await _drafts_of(user_id, message, services),
        clarifications=tuple(question for question in asked if question.message_id == message.id),
    )


async def _drafts_of(
    user_id: UUID, message: ChatMessage, services: ReplyServices
) -> tuple[StoredDraft, ...]:
    shown = await services.drafts.list_shown(user_id, message.conversation_id)
    return tuple(draft for draft in shown if draft.message_id == message.id)
