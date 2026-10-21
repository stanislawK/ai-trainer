"""The assistant's reply to an athlete's message (PRD-0003 F1, B13, B14, G3; ADR-0008 skeleton
routing): routed by the Jev fast path or else the LLM router, while a Jev safety screen runs
alongside (ADR-0020), dispatched safety first,
streamed, and saved only once all of it came through, so a retry after a failure always
streams a fresh reply. A log for a sport whose plugin names an extraction template becomes a
pending draft the athlete confirms later (PRD-0004 B1, F2, ADR-0022), offered as an addition to
their latest session in that sport when it came close after it (B16, ADR-0014). A log whose
sport stays split between two or more of the athlete's sports is a choice card first, and the
draft follows the tap (B17, B23, ADR-0015). A pending draft's sport can be changed on its card,
which extracts the same log again in the new sport (B23, ticket #163)."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

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
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import (
    DraftSportNotOfferedError,
    SessionDraft,
    StoredDraft,
    joins_session,
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
    plan = _plan(with_screened_safety(routing.intents, pain=pain), _extractable(services.sports))
    for part in plan:
        if answers:
            yield ReplyChunk(PART_BREAK)
        if part.specialist == "not_yet":
            fixed = services.conversation.not_yet()
            yield ReplyChunk(fixed.text)
            answers.append(_Answer(fixed.text, fixed.template, part.sports))
            continue
        if part.specialist == "extract":
            answer = await _extract(context, part.sports[0], services)
            if isinstance(answer, ReplyFailed):
                yield answer
                return
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


async def _extract(
    context: ReplyContext, sport_id: str, services: ReplyServices
) -> _Answer | ReplyFailed:
    """The sport's extraction, as a draft in the athlete's timezone (ADR-0014, ADR-0022) under
    a fixed intro line. A failed call ends the reply; nothing of it is kept."""
    extraction = await services.conversation.extract(context, sport_id)
    if extraction.output is None:
        return ReplyFailed(outcome=extraction.outcome)
    spec = services.sports.get(sport_id).extraction
    assert spec is not None  # `_plan` only sends sports that name an extraction
    draft = SessionDraft(
        occurred_on=extraction.output.occurred_on,
        date_source=extraction.output.date_source,
        timezone=context.timezone,
        payload=extraction.output.to_payload().model_dump(mode="json"),
    )
    intro = services.conversation.draft_ready()
    return _Answer(
        intro.text,
        extraction.template,
        (sport_id,),
        draft=(sport_id, draft, spec.payload_version),
    )


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
    message was sent within the merge window of that session's latest log (B16). `after` stamps
    the parts after a reply already saved to the message, as a draft after its question."""
    saved: list[ChatMessage] = []
    drafts: list[StoredDraft] = []
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
            extends = await _extended_session(user_id, message, sport_id, draft, services)
            drafts.append(
                await services.drafts.add(
                    user_id,
                    message.conversation_id,
                    message.id,
                    sport_id,
                    draft,
                    payload_version=payload_version,
                    created_at=services.clock.now(),
                    extends_session_id=extends,
                )
            )
        await services.conversations.touch(user_id, message.conversation_id, services.clock.now())
    return ReplyDone(messages=saved, drafts=tuple(drafts))


async def _extended_session(
    user_id: UUID,
    message: ChatMessage,
    sport_id: str,
    draft: SessionDraft,
    services: ReplyServices,
) -> UUID | None:
    """The athlete's latest session in the sport, when the log was sent within the merge
    window of its latest log, on a day the draft can belong to (B16)."""
    latest = await services.sessions.latest_in_sport(user_id, sport_id)
    if latest is not None and joins_session(
        draft, message.created_at, latest, services.merge_window
    ):
        return latest.id
    return None


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
class ClarificationAnswered:
    """The question as answered, and the reply its answer resumed: the draft's intro line and
    the pending draft in the chosen sport."""

    clarification: StoredClarification
    reply: ReplyDone


def _chosen_sport(question: StoredClarification, option_ids: Sequence[str]) -> str:
    """The one sport the athlete tapped. The option ID is trusted, label text never is: an ID
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
) -> ClarificationAnswered | ReplyFailed:
    """Resumes the extraction the question blocked with the tapped sport (B23, ADR-0015). The
    question is answered, and its draft stored, only once the extraction came through, so a
    failed one leaves the card to tap again; two taps at once leave one draft, because the
    second finds the question already answered before anything is written. Raises
    `ClarificationNotFoundError`, `ClarificationResolvedError` or `UnknownOptionError`
    (nothing is called or written then), and `MessageNotFoundError` for a log past its
    conversation's retention."""
    question = await services.clarifications.get(user_id, clarification_id)
    if question.purpose != SPORT_PURPOSE:
        raise ClarificationNotFoundError(clarification_id)
    if question.status != "open":
        raise ClarificationResolvedError(clarification_id)
    sport = _chosen_sport(question, option_ids)
    message = await find_message(
        user_id, question.message_id, chat=services.chat, conversations=services.conversations
    )
    answer = await _extract(await _context(user_id, message, timezone, services), sport, services)
    if isinstance(answer, ReplyFailed):
        return answer

    async def resume() -> ClarificationAnswered:
        resolved = await services.clarifications.resolve(
            user_id,
            clarification_id,
            "answered",
            option_ids=[sport],
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


async def abandon_clarification(
    user_id: UUID, clarification_id: UUID, *, services: ReplyServices
) -> StoredClarification:
    """ "Let me type instead" (F9): closes the question with no answer, and nothing else is
    written. Raises `ClarificationNotFoundError` or `ClarificationResolvedError`."""
    return await services.clarifications.resolve(
        user_id,
        clarification_id,
        "abandoned",
        option_ids=None,
        resolved_at=services.clock.now(),
    )


def sports_to_switch_to(
    athlete_sports: Sequence[str], current: str, sports: SportRegistry
) -> tuple[str, ...]:
    """The sports a draft's card offers to move it to (B23, ticket #163): the athlete's own, in
    their order, that name an extraction template, without the draft's own sport."""
    loggable = _extractable(sports)
    return tuple(sport for sport in athlete_sports if sport in loggable and sport != current)


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
    (ADR-0022 invariant 3), and a failed extraction changes nothing. Raises
    `DraftNotFoundError`, `DraftConfirmedError` or `DraftDiscardedError`, and
    `DraftSportNotOfferedError` for a sport the card never offered (nothing is called then), and
    `MessageNotFoundError` for a log past its conversation's retention."""
    stored = await draft_to_edit(user_id, draft_id, drafts=services.drafts)
    offered = sports_to_switch_to(
        await services.profile.list_sports(user_id), stored.sport_id, services.sports
    )
    if sport_id not in offered:
        raise DraftSportNotOfferedError(sport_id)
    message = await find_message(
        user_id, stored.message_id, chat=services.chat, conversations=services.conversations
    )
    # Read as of when the log was sent, as its first extraction was, so a switch after
    # midnight still dates "yesterday" or an unstated day from the log (ADR-0014).
    context = replace(await _context(user_id, message, timezone, services), now=message.created_at)
    answer = await _extract(context, sport_id, services)
    if isinstance(answer, ReplyFailed):
        return answer
    assert answer.draft is not None  # an extraction's answer always carries its draft
    _, draft, payload_version = answer.draft

    async def replace_draft() -> StoredDraft:
        return await services.drafts.change_sport(
            user_id,
            draft_id,
            sport_id,
            draft,
            payload_version=payload_version,
            extends_session_id=await _extended_session(user_id, message, sport_id, draft, services),
        )

    # Saved in its own task: a reader who leaves now can't cut the replacement off.
    return await asyncio.shield(asyncio.ensure_future(replace_draft()))
