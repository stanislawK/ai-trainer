"""The assistant's reply to an athlete's message (PRD-0003 F1, B13, B14, G3; ADR-0008 skeleton
routing): routed by the Jev fast path or else the LLM router, while a Jev safety screen runs
alongside (ADR-0020), dispatched safety first,
streamed, and saved only once all of it came through, so a retry after a failure always
streams a fresh reply. A log for a sport whose plugin names an extraction template becomes a
pending draft the athlete confirms later (PRD-0004 B1, F2, ADR-0022), offered as an addition to
their latest session in that sport when it came close after it (B16, ADR-0014). A log whose
sport stays split between two or more of the athlete's sports is a choice card first, and the
draft follows the tap (B17, B23, ADR-0015). A pending draft's sport can be changed on its card,
which extracts the same log again in the new sport (B23, ticket #163). A log of several sports
runs their extractions at once, one draft each in message order; one that fails beside a part
that came through is kept as a line its Retry can turn into the draft (B1, B14, ticket #161).
A log beyond the merge window, on the day of the athlete's latest session in its sport, is a
pending draft whose card asks whether it is that session, and the tap settles it (B16, ticket
#166). A log its sport's extraction can't read safely is a choice card of its readings, and the
tap resumes that extraction with the pick, which can then only draft (B17, ticket #164)."""

import asyncio
from collections.abc import AsyncGenerator, Collection, Iterable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from ai_trainer.application.drafts import draft_to_edit
from ai_trainer.application.ports.chat import ChatRepositoryPort, ConversationRepositoryPort
from ai_trainer.application.ports.clarifications import ClarificationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.conversation import (
    ConversationModelPort,
    ReplyContext,
    Routing,
    TemplateRef,
)
from ai_trainer.application.ports.drafts import SessionDraftRepositoryPort
from ai_trainer.application.ports.fast_router import FastRoute, FastRouterPort
from ai_trainer.application.ports.llm_gateway import StreamEnd
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.application.ports.safety_screen import SafetyScreenPort
from ai_trainer.application.ports.training import TrainingSessionRepositoryPort
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import (
    NEW_SESSION_OPTION,
    READING_PURPOSE,
    SAME_SESSION_ADD_COPY_KEY,
    SAME_SESSION_NEW_COPY_KEY,
    SAME_SESSION_PURPOSE,
    SPORT_PURPOSE,
    ChoiceOption,
    Clarification,
    ClarificationNotFoundError,
    ClarificationResolvedError,
    StoredClarification,
    UnknownOptionError,
)
from ai_trainer.domain.conversation import Intent, IntentKind
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.base import ExtractionQuestion
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import (
    DRAFT_FAILED_TEMPLATE_ID,
    Continuity,
    DraftSportNotOfferedError,
    SessionDraft,
    SessionSpan,
    StoredDraft,
    continuity,
)

# Until each specialist ships, these go to `chitchat`; the persona carries G3 (ADR-0008).
_CHITCHAT_KINDS = frozenset({"chitchat", "unclear", "wellbeing_or_injury"})
_SAFETY_KIND: IntentKind = "wellbeing_or_injury"
_LOG_KIND: IntentKind = "log_session"
# Streamed between two parts of one reply, which are saved as two messages.
PART_BREAK = "\n\n"
# Each reply part is stamped this far after what it follows, so it sorts right under the
# message it answers even when the athlete sent another message before it came through.
_TICK = timedelta(microseconds=1)
# A question needs at least two options (ADR-0015).
_MIN_OPTIONS = 2


class MessageNotFoundError(Exception):
    """Raised for a message that doesn't exist, isn't the user's, or isn't theirs to answer."""


class FailedPartNotFoundError(Exception):
    """Raised for a retry of a reply part that isn't the user's failed draft, or was already
    retried (ticket #161)."""


@dataclass(frozen=True, slots=True)
class ReplyPhase:
    """The step the reply is on, so the chat can name it before the first token (F14)."""

    phase: Literal["routing", "replying"]


@dataclass(frozen=True, slots=True)
class ReplyChunk:
    text: str


@dataclass(frozen=True, slots=True)
class ReplyDone:
    """The whole reply, as saved: one message per part, in the order they were streamed, the
    pending drafts made from the message, and the questions it asks (ADR-0015)."""

    messages: Sequence[ChatMessage]
    drafts: Sequence[StoredDraft] = ()
    clarifications: Sequence[StoredClarification] = ()


@dataclass(frozen=True, slots=True)
class ReplyFailed:
    """The router or a specialist timed out or failed; nothing was saved."""

    outcome: LlmCallOutcome


type ReplyEvent = ReplyPhase | ReplyChunk | ReplyDone | ReplyFailed


@dataclass(frozen=True, slots=True)
class ReplyServices:
    chat: ChatRepositoryPort
    conversations: ConversationRepositoryPort
    drafts: SessionDraftRepositoryPort
    clarifications: ClarificationRepositoryPort
    sessions: TrainingSessionRepositoryPort
    sports: SportRegistry
    profile: OnboardingRepositoryPort
    conversation: ConversationModelPort
    fast_router: FastRouterPort
    safety_screen: SafetyScreenPort
    clock: ClockPort
    # The router and every specialist see this many turns before the message, from its own
    # conversation only (ADR-0008, ADR-0021).
    history_turns: int
    # A log this close after the latest log of a session in its sport is offered as an addition
    # to it (`session_merge_window_min`, B16).
    merge_window: timedelta
    # A question offers at most this many options (`max_clarification_options`, ADR-0015).
    max_clarification_options: int
    # A sport's extraction reads its context from this many of the athlete's latest sessions in
    # it (`extract_recent_sessions`, ticket #164).
    recent_sessions: int


type _Specialist = Literal["chitchat", "extract", "not_yet"]


@dataclass(frozen=True, slots=True)
class _Part:
    specialist: _Specialist
    sports: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Answer:
    text: str
    template: TemplateRef
    sports: tuple[str, ...]
    # The draft an extraction part made, with its sport and payload version.
    draft: tuple[str, SessionDraft, int] | None = None
    # The question an extraction part asked instead of a draft (ticket #164).
    question: Clarification | None = None


def with_screened_safety(intents: Sequence[Intent], *, pain: bool) -> list[Intent]:
    """The router's intents, plus the safety intent when the screen found pain the router
    missed. The screen only ever adds it, never removes or reorders an intent (ADR-0020
    invariant 5); `_plan` puts it first."""
    if pain and all(intent.kind != _SAFETY_KIND for intent in intents):
        return [*intents, Intent(kind=_SAFETY_KIND)]
    return list(intents)


def _plan(intents: Sequence[Intent], extractable: frozenset[str]) -> list[_Part]:
    """One part per specialist, safety first and then in message order (ADR-0008 invariant 4).
    Intents bound for the same specialist share one part: the chitchat call sees the whole
    message, one extraction per sport sees it too, and one "not yet" reply covers every intent
    that has no specialist yet. `extractable` are the sports whose plugin names an extraction
    template (ADR-0006)."""
    ordered = sorted(intents, key=lambda intent: intent.kind != _SAFETY_KIND)
    keys: list[tuple[_Specialist, str | None]] = []
    not_yet_sports: list[str] = []
    for intent in ordered:
        key: tuple[_Specialist, str | None]
        if intent.kind in _CHITCHAT_KINDS:
            key = ("chitchat", None)
        elif intent.kind == _LOG_KIND and intent.sport in extractable:
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


def _extractable(sports: SportRegistry) -> frozenset[str]:
    return frozenset(plugin.id for plugin in sports.all() if plugin.extraction is not None)


async def find_message(
    user_id: UUID,
    message_id: UUID,
    *,
    chat: ChatRepositoryPort,
    conversations: ConversationRepositoryPort,
) -> ChatMessage:
    """The athlete's own message, the only kind that gets a reply. One in a conversation past
    its retention window is not found, though the purge hasn't removed it yet (ADR-0021)."""
    message = await chat.get(user_id, message_id)
    if (
        message is None
        or message.role is not ChatRole.USER
        or await conversations.get(user_id, message.conversation_id) is None
    ):
        raise MessageNotFoundError
    return message


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

    context = await _context(user_id, message, timezone, services)
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
        asking = asyncio.ensure_future(_ask_sport(user_id, message, routing.sport_choice, services))
        yield await asyncio.shield(asking)
        return

    yield ReplyPhase("replying")

    answers: list[_Answer] = []
    failures: list[ReplyFailed] = []
    came_through = False
    plan = _plan(with_screened_safety(routing.intents, pain=pain), _extractable(services.sports))
    # Every extraction starts now, so a log of two sports waits only as long as the slower one
    # (G14); each part is still streamed in its place.
    extractions = {
        part.sports[0]: asyncio.ensure_future(_extract(context, part.sports[0], services))
        for part in plan
        if part.specialist == "extract"
    }
    try:
        for part in plan:
            if answers:
                yield ReplyChunk(PART_BREAK)
            if part.specialist == "not_yet":
                fixed = services.conversation.not_yet()
                yield ReplyChunk(fixed.text)
                answers.append(_Answer(fixed.text, fixed.template, part.sports))
                continue
            if part.specialist == "extract":
                answer = await extractions[part.sports[0]]
                if isinstance(answer, ReplyFailed):
                    failures.append(answer)
                    answer = _draft_failed(part.sports[0], services)
                else:
                    came_through = True
                yield ReplyChunk(answer.text)
                answers.append(answer)
                continue
            async with aclosing(services.conversation.stream_chitchat(context)) as stream:
                async for event in stream:
                    if not isinstance(event, StreamEnd):
                        yield ReplyChunk(event.text)
                    elif event.result.output is None:
                        yield ReplyFailed(outcome=event.result.outcome)
                        return
                    else:
                        template = services.conversation.chitchat_template
                        answers.append(_Answer(event.result.output, template, part.sports))
                        came_through = True
    finally:
        for extraction in extractions.values():
            extraction.cancel()
    if failures and not came_through:
        # Nothing the models made came through: the reply fails whole, and a retry streams a
        # fresh one (ADR-0008).
        yield failures[0]
        return

    # Saved in its own task: a reader who leaves now can't cut a reply off halfway through.
    saving = asyncio.ensure_future(_save(user_id, message, answers, services))
    yield await asyncio.shield(saving)


async def _context(
    user_id: UUID, message: ChatMessage, timezone: str, services: ReplyServices
) -> ReplyContext:
    return ReplyContext(
        user_id=user_id,
        conversation_id=message.conversation_id,
        message=message.text,
        history=await services.chat.list_before(user_id, message, services.history_turns),
        athlete_sports=await services.profile.list_sports(user_id),
        goals=await services.profile.list_goals(user_id),
        now=services.clock.now(),
        timezone=timezone,
    )


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


async def route_message(context: ReplyContext, services: ReplyServices) -> Routing:
    """One confident intent from the fast path, else a follow-up log in the conversation's
    sport, else the LLM router (ADR-0008 amendment, ADR-0026 step 5). A declined message pays
    the fast path's wait, capped by its timeout, before the router, unless the sport is still
    split between two or more loggable sports: then the athlete is asked (B23, ADR-0015).
    Either way the routing carries Jev's plausible sports when it gave them (B23), and the
    router's own logged sports when it did not."""
    fast = await services.fast_router.route(context)
    if fast.intents is not None:
        return Routing(
            intents=fast.intents,
            outcome=LlmCallOutcome.SUCCESS,
            plausible_sports=fast.plausible_sports or (),
        )
    sport = await _conversation_sport(context, fast, services)
    if sport is not None:
        return Routing(
            intents=[Intent(kind=_LOG_KIND, sport=sport)],
            outcome=LlmCallOutcome.SUCCESS,
            plausible_sports=fast.plausible_sports or (),
        )
    choice = _sport_choice(fast, services)
    if choice:
        # The likeliest sport stands in for the intent, for a reply that cannot wait on the
        # athlete's tap (a pain mention); the router is not called, since it would only guess.
        return Routing(
            intents=[Intent(kind=_LOG_KIND, sport=choice[0])],
            outcome=LlmCallOutcome.SUCCESS,
            plausible_sports=fast.plausible_sports or (),
            sport_choice=choice,
        )
    routed = await services.conversation.route(context)
    if not fast.plausible_sports:
        # Jev gave none, or a floor above every sport left nothing in play.
        return replace(routed, plausible_sports=_logged_sports(routed.intents))
    return replace(routed, plausible_sports=fast.plausible_sports)


def _sport_choice(fast: FastRoute, services: ReplyServices) -> tuple[str, ...]:
    """The sports to ask between: one log whose sport Jev left split, and two or more of the
    plausible sports can be logged, likeliest first, cut to `max_clarification_options`. A sport
    with no extraction template is not offered, since the athlete could not get a draft."""
    if fast.single_intent != _LOG_KIND or not fast.plausible_sports:
        return ()
    loggable = _extractable(services.sports)
    options = tuple(sport for sport in fast.plausible_sports if sport in loggable)
    return options[: services.max_clarification_options] if len(options) >= _MIN_OPTIONS else ()


async def _conversation_sport(
    context: ReplyContext, fast: FastRoute, services: ReplyServices
) -> str | None:
    """The sport of the conversation's latest draft, for a message Jev is sure is one log
    whose sport it could not settle: only when that draft's log was sent within the merge
    window, the boundary included, as B16 measures it between logs, and its sport is among
    Jev's plausible ones (ADR-0026 invariant 5). A discarded draft gives no sport: the athlete
    may have discarded it for its sport."""
    if fast.single_intent != _LOG_KIND or not fast.plausible_sports:
        return None
    shown = await services.drafts.list_shown(context.user_id, context.conversation_id)
    kept = [draft for draft in shown if draft.status != "discarded"]
    if not kept:
        return None
    latest = max(kept, key=lambda draft: draft.created_at)
    if latest.sport_id not in fast.plausible_sports:
        return None
    log = await services.chat.get(context.user_id, latest.message_id)
    if log is None or context.now - log.created_at > services.merge_window:
        return None
    return latest.sport_id


async def _recent(user_id: UUID, sport_id: str, services: ReplyServices) -> tuple[str, ...]:
    """The sport's context from the athlete's latest sessions in it, as its plugin reads it
    (ticket #164); none for a plugin that reads none."""
    spec = services.sports.get(sport_id).extraction
    if spec is None or spec.recent is None:
        return ()
    return spec.recent(
        await services.sessions.recent_payloads(user_id, sport_id, services.recent_sessions)
    )


async def _extract(
    context: ReplyContext,
    sport_id: str,
    services: ReplyServices,
    *,
    answer: ChoiceOption | None = None,
    may_ask: bool = True,
) -> _Answer | ReplyFailed:
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
    assert spec is not None  # `_plan` only sends sports that name an extraction
    # The athlete's pick holds whatever the model made of it (ADR-0015 invariant 4).
    output = extraction.output if answer is None else extraction.output.answered(answer)
    draft = SessionDraft(
        occurred_on=output.occurred_on,
        date_source=output.date_source,
        timezone=context.timezone,
        payload=output.to_payload().model_dump(mode="json"),
    )
    intro = services.conversation.draft_ready()
    return _Answer(
        intro.text,
        extraction.template,
        (sport_id,),
        draft=(sport_id, draft, spec.payload_version),
    )


def _reading_question(
    sport_id: str, question: ExtractionQuestion, services: ReplyServices
) -> _Answer:
    """The question's fixed line, and the question with one option per reading, likeliest
    first, cut to `max_clarification_options` (ADR-0015). Nothing but the question is written
    before the tap (invariant 3)."""
    fixed = services.conversation.reading_question()
    return _Answer(
        fixed.text,
        fixed.template,
        (sport_id,),
        question=Clarification(
            question=fixed.text,
            options=question.options()[: services.max_clarification_options],
            pending=_LOG_KIND,
            sport=sport_id,
        ),
    )


def _draft_failed(sport_id: str, services: ReplyServices) -> _Answer:
    """The line saved in place of the sport's draft when its extraction failed beside a part
    that came through, so the athlete can retry that sport alone (ticket #161)."""
    fixed = services.conversation.draft_failed()
    return _Answer(fixed.text, fixed.template, (sport_id,))


async def _drafts_of(
    user_id: UUID, message: ChatMessage, services: ReplyServices
) -> tuple[StoredDraft, ...]:
    shown = await services.drafts.list_shown(user_id, message.conversation_id)
    return tuple(draft for draft in shown if draft.message_id == message.id)


def _logged_sports(intents: Sequence[Intent]) -> tuple[str, ...]:
    """The router's own sports, in message order, each once."""
    sports: dict[str, None] = {}
    for intent in intents:
        if intent.kind == _LOG_KIND and intent.sport is not None:
            sports.setdefault(intent.sport)
    return tuple(sports)


async def _save(
    user_id: UUID,
    message: ChatMessage,
    answers: Sequence[_Answer],
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
        stamp += _TICK
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
            stored, question = await _store_draft(
                user_id, message, sport_id, draft, payload_version, services
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


async def _continuity(
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


async def _store_draft(
    user_id: UUID,
    message: ChatMessage,
    sport_id: str,
    draft: SessionDraft,
    payload_version: int,
    services: ReplyServices,
) -> tuple[StoredDraft, StoredClarification | None]:
    """The pending draft, extending the session it joins, and the same-session question on
    its card when it came later that day, stored with it in one transaction; the question
    extends nothing (ADR-0014 invariant 5)."""
    kind, latest = await _continuity(user_id, message, sport_id, draft, services)
    stored = await services.drafts.add(
        user_id,
        message.conversation_id,
        message.id,
        sport_id,
        draft,
        payload_version=payload_version,
        created_at=services.clock.now(),
        extends_session_id=latest.id if kind == "join" and latest is not None else None,
        asks=_same_session_question(draft, latest, services)
        if kind == "ask" and latest is not None
        else None,
    )
    return stored, await _question_on(user_id, stored, services)


def _same_session_question(
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
        pending=_LOG_KIND,
    )


async def _question_on(
    user_id: UUID, draft: StoredDraft, services: ReplyServices
) -> StoredClarification | None:
    """The same-session question still open on the draft's card, if it asks one."""
    asked = await services.clarifications.list_shown(user_id, draft.conversation_id)
    return next(
        (
            question
            for question in asked
            if question.draft_id == draft.id
            and question.purpose == SAME_SESSION_PURPOSE
            and question.status == "open"
        ),
        None,
    )


async def _ask_sport(
    user_id: UUID, message: ChatMessage, sports: Sequence[str], services: ReplyServices
) -> ReplyDone:
    """The question as the reply to the athlete's log, and its open `clarifications` row. No
    draft and no session is written before the athlete picks (ADR-0015 invariant 3)."""
    question = services.conversation.sport_question()
    saved = await services.chat.add(
        user_id,
        message.conversation_id,
        ChatRole.ASSISTANT,
        question.text,
        message.created_at + _TICK,
        template_id=question.template.id,
        template_version=question.template.version,
    )
    asked = await services.clarifications.add(
        user_id,
        message.conversation_id,
        message.id,
        SPORT_PURPOSE,
        Clarification(
            question=question.text,
            options=tuple(
                ChoiceOption(id=sport, copy_key=services.sports.get(sport).label_key)
                for sport in sports
            ),
            pending=_LOG_KIND,
        ),
        created_at=services.clock.now(),
    )
    await services.conversations.touch(user_id, message.conversation_id, services.clock.now())
    return ReplyDone(messages=[saved], clarifications=(asked,))


@dataclass(frozen=True, slots=True)
class SessionSettled:
    """A same-session question as answered: the pending draft, now extending the session the
    athlete picked, or none (ticket #166)."""

    draft: StoredDraft


@dataclass(frozen=True, slots=True)
class ClarificationAnswered:
    """The question as answered, and the reply its answer resumed: the draft's intro line and
    the pending draft in the chosen sport."""

    clarification: StoredClarification
    reply: ReplyDone


def _chosen(question: StoredClarification, option_ids: Sequence[str]) -> str:
    """The one option the athlete tapped. The option ID is trusted, label text never is: an ID
    the question did not offer is an error, not a guess (ADR-0015 invariant 4)."""
    offered = {option.id for option in question.clarification.options}
    if len(option_ids) != 1 or option_ids[0] not in offered:
        raise UnknownOptionError(option_ids)
    return option_ids[0]


async def answer_clarification(
    user_id: UUID,
    clarification_id: UUID,
    option_ids: Sequence[str],
    *,
    timezone: str,
    services: ReplyServices,
) -> ClarificationAnswered | SessionSettled | ReplyFailed:
    """Resumes the extraction the question blocked with the tapped sport (B23, ADR-0015), or,
    for a reading question, in the sport that asked with the tapped reading, which can then only
    draft (ticket #164). The question is answered, and its reply stored, only once the
    extraction came through, so a failed one leaves the card to tap again; two taps at once
    leave one draft, because the second finds the question already answered before anything is
    written. A sport answered may ask how to read the log next. A same-session question settles
    its draft instead (ticket #166). Raises `ClarificationNotFoundError`,
    `ClarificationResolvedError` or `UnknownOptionError` (nothing is called or written then),
    and `MessageNotFoundError` for a log past its conversation's retention."""
    question = await services.clarifications.get(user_id, clarification_id)
    if question.purpose not in (SPORT_PURPOSE, READING_PURPOSE, SAME_SESSION_PURPOSE):
        raise ClarificationNotFoundError(clarification_id)
    if question.purpose == READING_PURPOSE and question.clarification.sport is None:
        raise ClarificationNotFoundError(clarification_id)
    if question.status != "open":
        raise ClarificationResolvedError(clarification_id)
    if question.purpose == SAME_SESSION_PURPOSE:
        return await _settle_session(user_id, question, option_ids, services)
    picked = _chosen(question, option_ids)
    reading = question.purpose == READING_PURPOSE
    sport = question.clarification.sport if reading else picked
    assert sport is not None  # a reading question names its sport, checked above
    message = await find_message(
        user_id, question.message_id, chat=services.chat, conversations=services.conversations
    )
    # Read as of when the log was sent, as a sport change and a retry are, so an answer the
    # next morning still dates "last night" from the log (ADR-0014, ticket #166).
    context = replace(await _context(user_id, message, timezone, services), now=message.created_at)
    option = next(option for option in question.clarification.options if option.id == picked)
    answer = await _extract(context, sport, services, answer=option if reading else None)
    if isinstance(answer, ReplyFailed):
        return answer

    async def resume() -> ClarificationAnswered:
        resolved = await services.clarifications.resolve(
            user_id,
            clarification_id,
            "answered",
            option_ids=[picked],
            resolved_at=services.clock.now(),
        )
        asked = await services.chat.list_replies(user_id, message)
        try:
            reply = await _save(
                user_id,
                message,
                [answer],
                services,
                after=max((part.created_at for part in asked), default=None),
            )
        except BaseException:
            # The draft did not come through, so the card goes back to be tapped again.
            await asyncio.shield(services.clarifications.reopen(user_id, clarification_id))
            raise
        return ClarificationAnswered(clarification=resolved, reply=reply)

    # Saved in its own task: a reader who leaves now can't cut the draft off after the answer.
    return await asyncio.shield(asyncio.ensure_future(resume()))


async def _settle_session(
    user_id: UUID,
    question: StoredClarification,
    option_ids: Sequence[str],
    services: ReplyServices,
) -> SessionSettled:
    """The tap on a draft's same-session question (B16, ticket #166): "Add" makes the draft
    extend the session the option names, "New session" makes it extend none. Nothing else is
    written; the athlete still confirms (ADR-0014 invariant 5)."""
    picked = _chosen(question, option_ids)
    if question.draft_id is None:
        raise ClarificationNotFoundError(question.id)
    draft = await services.drafts.settle_session(
        user_id,
        question.draft_id,
        question.id,
        picked,
        extends_session_id=None if picked == NEW_SESSION_OPTION else UUID(picked),
        answered_at=services.clock.now(),
    )
    return SessionSettled(draft=draft)


async def abandon_clarification(
    user_id: UUID, clarification_id: UUID, *, services: ReplyServices
) -> StoredClarification:
    """ "Let me type instead" (F9): closes the question with no answer, and nothing else is
    written. A question that offers no such escape, as a same-session one (ticket #166), can't
    be skipped this way, so it is not found. Raises `ClarificationNotFoundError` or
    `ClarificationResolvedError`."""
    question = await services.clarifications.get(user_id, clarification_id)
    if not question.clarification.allow_free_text:
        raise ClarificationNotFoundError(clarification_id)
    return await services.clarifications.resolve(
        user_id,
        clarification_id,
        "abandoned",
        option_ids=None,
        resolved_at=services.clock.now(),
    )


def sports_to_switch_to(
    athlete_sports: Sequence[str],
    current: str,
    sports: SportRegistry,
    *,
    asking: Collection[str] = (),
) -> tuple[str, ...]:
    """The sports a draft's card offers to move it to (B23, ticket #163): the athlete's own, in
    their order, that name an extraction template, without the draft's own sport, nor one whose
    reading question is still open on the same log (`asking`), which a switch would answer
    with a guess (ticket #164)."""
    loggable = _extractable(sports)
    return tuple(
        sport
        for sport in athlete_sports
        if sport in loggable and sport != current and sport not in asking
    )


def asking_sports(questions: Iterable[StoredClarification], message_id: UUID) -> frozenset[str]:
    """The sports whose reading question is still open on the athlete's log (ticket #164)."""
    return frozenset(
        question.clarification.sport
        for question in questions
        if question.message_id == message_id
        and question.purpose == READING_PURPOSE
        and question.status == "open"
        and question.clarification.sport is not None
    )


async def change_draft_sport(
    user_id: UUID,
    draft_id: UUID,
    sport_id: str,
    *,
    timezone: str,
    services: ReplyServices,
) -> StoredDraft | ReplyFailed:
    """The card's sport chip (B23, F2, ticket #163): re-runs `sport_id`'s extraction on the
    draft's own log and replaces the pending draft in place, offered again as an addition to
    the new sport's latest session when it came close after it (B16). No session is written
    (ADR-0022 invariant 3), and a failed extraction changes nothing. A same-session question
    on the card is dropped, and the new sport's own is asked when it came later that day
    (ticket #166). Raises `DraftNotFoundError`, `DraftConfirmedError` or `DraftDiscardedError`,
    and `DraftSportNotOfferedError` for a sport the card never offered (nothing is called then),
    and `MessageNotFoundError` for a log past its conversation's retention."""
    stored = await draft_to_edit(user_id, draft_id, drafts=services.drafts)
    asked = await services.clarifications.list_shown(user_id, stored.conversation_id)
    offered = sports_to_switch_to(
        await services.profile.list_sports(user_id),
        stored.sport_id,
        services.sports,
        asking=asking_sports(asked, stored.message_id),
    )
    if sport_id not in offered:
        raise DraftSportNotOfferedError(sport_id)
    message = await find_message(
        user_id, stored.message_id, chat=services.chat, conversations=services.conversations
    )
    # Read as of when the log was sent, as its first extraction was, so a switch after
    # midnight still dates "yesterday" or an unstated day from the log (ADR-0014).
    context = replace(await _context(user_id, message, timezone, services), now=message.created_at)
    # The card's own fields fix how the log reads, so the switch never asks (ticket #164).
    answer = await _extract(context, sport_id, services, may_ask=False)
    if isinstance(answer, ReplyFailed):
        return answer
    assert answer.draft is not None  # an extraction that may not ask always drafts
    _, draft, payload_version = answer.draft

    async def replace_draft() -> StoredDraft:
        kind, latest = await _continuity(user_id, message, sport_id, draft, services)
        return await services.drafts.change_sport(
            user_id,
            draft_id,
            sport_id,
            draft,
            payload_version=payload_version,
            extends_session_id=latest.id if kind == "join" and latest is not None else None,
            asks=_same_session_question(draft, latest, services)
            if kind == "ask" and latest is not None
            else None,
            asked_at=services.clock.now(),
        )

    # Saved in its own task: a reader who leaves now can't cut the replacement off.
    return await asyncio.shield(asyncio.ensure_future(replace_draft()))


@dataclass(frozen=True, slots=True)
class RetriedDraft:
    """A failed draft's part as the retry rewrote it, the draft's intro line, and the pending
    draft it now introduces."""

    part: ChatMessage
    draft: StoredDraft


async def _failed_part(
    user_id: UUID, part_id: UUID, services: ReplyServices
) -> tuple[ChatMessage, ChatMessage]:
    """The user's failed draft's part and the log it replies to: the user message the reply's
    parts follow. Raises `FailedPartNotFoundError` for any other part, and
    `MessageNotFoundError` for a log past its conversation's retention."""
    part = await services.chat.get(user_id, part_id)
    if (
        part is None
        or part.role is not ChatRole.ASSISTANT
        or part.template_id != DRAFT_FAILED_TEMPLATE_ID
        or len(part.sports) != 1
    ):
        raise FailedPartNotFoundError(part_id)
    before = part
    while earlier := await services.chat.list_before(user_id, before, 1):
        before = earlier[0]
        if before.role is ChatRole.USER:
            log = await find_message(
                user_id, before.id, chat=services.chat, conversations=services.conversations
            )
            return part, log
    raise FailedPartNotFoundError(part_id)


@dataclass(frozen=True, slots=True)
class RetryFailed:
    """The retried extraction failed again: the part as it stays, to show its row again."""

    part: ChatMessage
    outcome: LlmCallOutcome


async def retry_draft(
    user_id: UUID, part_id: UUID, *, timezone: str, services: ReplyServices
) -> RetriedDraft | RetryFailed:
    """A failed draft's Retry (ticket #161): runs its sport's extraction again on its log, as of
    when the log was sent, and turns the part into the draft's intro line in place, with the
    pending draft under it, offered as an addition to a session close before it (B16), or
    asking whether it is one from earlier that day (ticket #166). No
    session is written (ADR-0022 invariant 3), and a failed extraction changes nothing. Two
    retries at once leave one draft: the second finds the part already rewritten. Raises
    `FailedPartNotFoundError` for a part that isn't the user's failed draft (nothing is called
    then), and `MessageNotFoundError` for a log past its conversation's retention."""
    failed, message = await _failed_part(user_id, part_id, services)
    sport_id = failed.sports[0]
    # Read as of when the log was sent, as its first extraction was (ADR-0014).
    context = replace(await _context(user_id, message, timezone, services), now=message.created_at)
    # A retry turns its line into a draft, never a question (ticket #164).
    answer = await _extract(context, sport_id, services, may_ask=False)
    if isinstance(answer, ReplyFailed):
        return RetryFailed(part=failed, outcome=answer.outcome)
    assert answer.draft is not None  # an extraction that may not ask always drafts
    _, draft, payload_version = answer.draft

    async def save() -> RetriedDraft:
        part = await services.chat.replace_part(
            user_id,
            part_id,
            expected_template_id=DRAFT_FAILED_TEMPLATE_ID,
            text=answer.text,
            template_id=answer.template.id,
            template_version=answer.template.version,
        )
        if part is None:
            raise FailedPartNotFoundError(part_id)
        try:
            stored, _ = await _store_draft(
                user_id, message, sport_id, draft, payload_version, services
            )
        except BaseException:
            # The draft did not come through, so the part goes back to offer Retry again.
            fixed = services.conversation.draft_failed()
            await asyncio.shield(
                services.chat.replace_part(
                    user_id,
                    part_id,
                    expected_template_id=answer.template.id,
                    text=fixed.text,
                    template_id=fixed.template.id,
                    template_version=fixed.template.version,
                )
            )
            raise
        await services.conversations.touch(user_id, message.conversation_id, services.clock.now())
        return RetriedDraft(part=part, draft=stored)

    # Saved in its own task: a reader who leaves now can't cut the draft off after its line.
    return await asyncio.shield(asyncio.ensure_future(save()))
