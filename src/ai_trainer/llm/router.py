"""The `router` template: classifies a message into intents (ADR-0008)."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, create_model
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from ai_trainer.domain.conversation import INTENT_KINDS
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.template import PromptTemplate

ROUTER_TEMPLATE_ID = "router"
# Every version with a body file stays registered so evals can compare them (ADR-0008
# invariant 2); the app runs the active one.
ROUTER_VERSIONS = (1, 2)
ACTIVE_ROUTER_VERSION = 2


_SPORT_INTENT_KIND = "log_session"


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class RouterDeps(BaseModel):
    """The router template's variables (ADR-0008): the message, the recent turns, the
    athlete's own sports and the time context."""

    message: str
    history: list[ChatTurn] = []
    athlete_sports: list[str] = []
    today: date
    now_local: datetime
    timezone: str


def recent_turns(turns: list[ChatTurn], *, limit: int) -> list[ChatTurn]:
    return turns[-limit:]


def build_router_deps(
    *,
    message: str,
    history: list[ChatTurn],
    athlete_sports: list[str],
    now: datetime,
    timezone: str,
    history_turns: int,
) -> RouterDeps:
    """`now` comes from the `Clock` port; `today` and `now_local` are resolved in the
    athlete's timezone so relative dates need no tool call (ADR-0014)."""
    now_local = now.astimezone(ZoneInfo(timezone))
    return RouterDeps(
        message=message,
        history=recent_turns(history, limit=history_turns),
        athlete_sports=athlete_sports,
        today=now_local.date(),
        now_local=now_local,
        timezone=timezone,
    )


def _intent_model(kind: str, **fields: Any) -> type[BaseModel]:
    """Fields in the order the model should decide them: `kind` first, `confidence` last, so
    the number is written after the intent is chosen."""
    name = "".join(part.capitalize() for part in kind.split("_")) + "Intent"
    return create_model(
        name,
        kind=(Literal[kind], ...),
        **fields,
        span=(str, ...),
        confidence=(float, Field(ge=0, le=1)),
    )


def router_output_type(registry: SportRegistry) -> type[BaseModel]:
    """`RouterOutput(intents: list[Intent])`, `Intent` a union discriminated on `kind`.
    `log_session.sport` is the registry's ids, so a new sport needs no code or template edit
    (ADR-0006, ADR-0008)."""
    sport_ids = tuple(plugin.id for plugin in registry.all())
    intent_models = [
        _intent_model(kind, sport=(Literal[sport_ids], ...))
        if kind == _SPORT_INTENT_KIND
        else _intent_model(kind)
        for kind in INTENT_KINDS
    ]
    intent: Any = Annotated[Union[tuple(intent_models)], Field(discriminator="kind")]  # noqa: UP007
    return create_model("RouterOutput", intents=(Annotated[list[intent], Field(min_length=1)], ...))


def router_template(
    registry: SportRegistry, *, version: int = ACTIVE_ROUTER_VERSION
) -> PromptTemplate[RouterDeps, BaseModel]:
    return PromptTemplate(
        id=ROUTER_TEMPLATE_ID,
        version=version,
        locale="en",
        deps_type=RouterDeps,
        output_type=router_output_type(registry),
        model_settings_key="router_model",
        provider_settings_key="router_provider",
        temperature=0,
        # One malformed reply in a 132-call eval run aborted the baseline write (#127).
        output_retries=2,
    )


def _intents(output: BaseModel) -> list[Any]:
    intents: list[Any] = getattr(output, "intents")  # noqa: B009 -- the model is built at runtime
    return intents


def _kinds(output: BaseModel) -> list[str]:
    return [intent.kind for intent in _intents(output)]


def _logged_sports(output: BaseModel) -> list[str]:
    return [intent.sport for intent in _intents(output) if intent.kind == _SPORT_INTENT_KIND]


def _strip_trailing(kinds: list[str], optional: list[str]) -> list[str]:
    end = len(kinds)
    while end > 0 and kinds[end - 1] in optional:
        end -= 1
    return kinds[:end]


def _collapse_repeats(kinds: list[str]) -> list[str]:
    """Consecutive repeats of one kind are one intent, except `log_session`, which can
    legitimately repeat in a message (B1)."""
    collapsed: list[str] = []
    for kind in kinds:
        if collapsed and collapsed[-1] == kind and kind != _SPORT_INTENT_KIND:
            continue
        collapsed.append(kind)
    return collapsed


@dataclass
class IntentKindsMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded: the same intent kinds in the same order (pain first, then message order),
    with consecutive repeats of any kind but `log_session` collapsed. A case may list
    `metadata.optional_trailing_kinds`; those kinds at the end of the output are ignored, so a
    pain case can pass with or without the log that follows it."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> bool:
        assert ctx.expected_output is not None
        optional = (ctx.metadata or {}).get("optional_trailing_kinds", [])
        return _collapse_repeats(_strip_trailing(_kinds(ctx.output), optional)) == (
            _collapse_repeats(_kinds(ctx.expected_output))
        )


@dataclass
class LogSessionSportMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B23): the share of expected `log_session` sports the router got right.
    A case may list `metadata.acceptable_sports`; any of them is a hit. Without it, grading is
    strict."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> float:
        assert ctx.expected_output is not None
        expected = _logged_sports(ctx.expected_output)
        if not expected:
            return 1.0
        actual = _logged_sports(ctx.output)
        acceptable = (ctx.metadata or {}).get("acceptable_sports")
        hits = sum(
            1
            for want, got in zip(expected, actual, strict=False)
            if got == want or (acceptable is not None and got in acceptable)
        )
        return hits / len(expected)
