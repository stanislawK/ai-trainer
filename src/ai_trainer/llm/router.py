"""The `router` template: classifies a message into intents (ADR-0008)."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, Union, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, create_model
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.template import PromptTemplate

ROUTER_TEMPLATE_ID = "router"

IntentKind = Literal[
    "log_session",
    "edit_session",
    "ask_training_question",
    "request_plan",
    "adjust_plan",
    "request_report",
    "update_profile",
    "wellbeing_or_injury",
    "chitchat",
    "unclear",
]
INTENT_KINDS: tuple[str, ...] = get_args(IntentKind)

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


class _IntentBase(BaseModel):
    confidence: float = Field(ge=0, le=1)
    span: str


def _intent_model(kind: str, **fields: Any) -> type[_IntentBase]:
    name = "".join(part.capitalize() for part in kind.split("_")) + "Intent"
    return create_model(name, __base__=_IntentBase, kind=(Literal[kind], ...), **fields)


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


def router_template(registry: SportRegistry) -> PromptTemplate[RouterDeps, BaseModel]:
    return PromptTemplate(
        id=ROUTER_TEMPLATE_ID,
        version=1,
        locale="en",
        deps_type=RouterDeps,
        output_type=router_output_type(registry),
        model_settings_key="router_model",
    )


def _intents(output: BaseModel) -> list[Any]:
    intents: list[Any] = getattr(output, "intents")  # noqa: B009 -- the model is built at runtime
    return intents


def _kinds(output: BaseModel) -> list[str]:
    return [intent.kind for intent in _intents(output)]


def _logged_sports(output: BaseModel) -> list[str]:
    return [intent.sport for intent in _intents(output) if intent.kind == _SPORT_INTENT_KIND]


@dataclass
class IntentKindsMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded: the same intent kinds in the same order (pain first, then message order)."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> bool:
        assert ctx.expected_output is not None
        return _kinds(ctx.output) == _kinds(ctx.expected_output)


@dataclass
class LogSessionSportMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B23): the share of expected `log_session` sports the router got right."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> float:
        assert ctx.expected_output is not None
        expected = _logged_sports(ctx.expected_output)
        if not expected:
            return 1.0
        actual = _logged_sports(ctx.output)
        hits = sum(1 for want, got in zip(expected, actual, strict=False) if want == got)
        return hits / len(expected)
