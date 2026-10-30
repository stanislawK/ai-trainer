"""Routing the athlete's message (ADR-0008 skeleton routing): the Jev fast path, else a
follow-up log in the conversation's sport, else the LLM router (ADR-0026 step 5), or the sports
to ask between when the log's sport stays split (B23, ADR-0015)."""

from collections.abc import Sequence
from dataclasses import replace

from ai_trainer.application.ports.conversation import ReplyContext, Routing
from ai_trainer.application.ports.fast_router import FastRoute
from ai_trainer.application.reply.shared import LOG_KIND, ReplyServices, extractable
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome

# A question needs at least two options (ADR-0015).
_MIN_OPTIONS = 2


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
            intents=[Intent(kind=LOG_KIND, sport=sport)],
            outcome=LlmCallOutcome.SUCCESS,
            plausible_sports=fast.plausible_sports or (),
        )
    choice = _sport_choice(fast, services)
    if choice:
        # The likeliest sport stands in for the intent, for a reply that cannot wait on the
        # athlete's tap (a pain mention); the router is not called, since it would only guess.
        return Routing(
            intents=[Intent(kind=LOG_KIND, sport=choice[0])],
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
    if fast.single_intent != LOG_KIND or not fast.plausible_sports:
        return ()
    loggable = extractable(services.sports)
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
    if fast.single_intent != LOG_KIND or not fast.plausible_sports:
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


def _logged_sports(intents: Sequence[Intent]) -> tuple[str, ...]:
    """The router's own sports, in message order, each once."""
    sports: dict[str, None] = {}
    for intent in intents:
        if intent.kind == LOG_KIND and intent.sport is not None:
            sports.setdefault(intent.sport)
    return tuple(sports)
