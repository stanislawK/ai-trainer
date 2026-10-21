"""Sport extraction templates (`<sport>.extract`): a log becomes a session draft (ADR-0006,
ADR-0008). Each plugin names its template and output model, so a new sport needs no edit here.
Dates come back absolute, with how they were found (ADR-0014). A plugin that names a question
lets its template return it instead of a draft, until the athlete has answered (ticket #164)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, field_validator
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, EvaluatorOutput

from ai_trainer.domain.sports.base import ExtractionQuestion, ExtractionSpec, SessionExtraction
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.template import PromptTemplate
from ai_trainer.llm.router import ChatTurn, recent_turns


class ExtractDeps(BaseModel):
    """Every extraction template's variables (ADR-0008): the message, the recent turns, and
    the time context (ADR-0014). `weekday` is computed here, so "on Tuesday" never depends on
    the model knowing what day a date falls on. History keeps only the athlete's own turns: the
    assistant's are fixed lines with no draft content (ADR-0026 step 6). This deps model drops
    them itself, so an eval case's deps read like the app's. `recent` is the sport's own context
    from the athlete's recent sessions, computed by its plugin, and `answers` the option IDs the
    athlete picked on the question this log asked (ticket #164)."""

    message: str
    history: list[ChatTurn] = []
    today: date
    weekday: str
    now_local: datetime
    timezone: str
    recent: list[str] = []
    answers: list[str] = []

    @field_validator("history")
    @classmethod
    def _athletes_turns_only(cls, history: list[ChatTurn]) -> list[ChatTurn]:
        return [turn for turn in history if turn.role == "user"]


def build_extract_deps(
    *,
    message: str,
    history: list[ChatTurn],
    now: datetime,
    timezone: str,
    history_turns: int,
    recent: Sequence[str] = (),
    answers: Sequence[str] = (),
) -> ExtractDeps:
    """`now` comes from the `Clock` port; the local day is resolved in the athlete's
    timezone (ADR-0014)."""
    now_local = now.astimezone(ZoneInfo(timezone))
    return ExtractDeps(
        message=message,
        history=recent_turns(history, limit=history_turns),
        today=now_local.date(),
        weekday=now_local.strftime("%A"),
        now_local=now_local,
        timezone=timezone,
        recent=list(recent),
        answers=list(answers),
    )


def extract_template(
    spec: ExtractionSpec, *, version: int | None = None, may_ask: bool = True
) -> PromptTemplate[ExtractDeps, SessionExtraction | ExtractionQuestion]:
    """The plugin's template at its active version, or at an earlier `version` for evals. From
    the version its question names, the output is the draft or that question, each its own
    output tool; a run that `may_ask` not, as one resumed with the athlete's answer, can only
    draft (ticket #164)."""
    at = spec.version if version is None else version
    asks = may_ask and spec.question is not None and at >= spec.question.since_version

    def answered_drafts_only(
        deps: BaseModel,
    ) -> type[SessionExtraction] | tuple[type[SessionExtraction | ExtractionQuestion], ...]:
        if (isinstance(deps, ExtractDeps) and deps.answers) or spec.question is None:
            return spec.output_type
        return (spec.output_type, spec.question.type)

    return PromptTemplate(
        id=spec.template_id,
        version=at,
        locale="en",
        deps_type=ExtractDeps,
        output_type=(spec.output_type, spec.question.type)
        if asks and spec.question is not None
        else spec.output_type,
        model_settings_key="extract_model",
        provider_settings_key="extract_provider_preferences",
        reasoning_effort_settings_key="extract_reasoning_effort",
        # The app reads the output: the same log should give the same draft twice.
        temperature=0,
        output_retries=2,
        # It never writes to the athlete (ADR-0026 step 6).
        persona=False,
        output_for=answered_drafts_only if asks else None,
    )


def extract_templates(sports: SportRegistry) -> list[PromptTemplate[ExtractDeps, BaseModel]]:
    """Every version of each sport's template, in registry order. Earlier versions stay
    registered so evals can compare them (ADR-0008 invariant 2); the app runs the active one."""
    return [
        extract_template(plugin.extraction, version=version)
        for plugin in sports.all()
        if plugin.extraction is not None
        for version in range(1, plugin.extraction.version + 1)
    ]


def _occurred(output: BaseModel) -> tuple[Any, Any]:
    return getattr(output, "occurred_on"), getattr(output, "date_source")  # noqa: B009


def _activity_keys(output: BaseModel, *, ignore: set[str]) -> list[str]:
    activities: list[BaseModel] = getattr(output, "activities")  # noqa: B009
    return sorted(
        json.dumps(
            {k: v for k, v in activity.model_dump(mode="json").items() if k not in ignore},
            sort_keys=True,
        )
        for activity in activities
    )


def _drafts(ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> bool | None:
    """Whether the output is a draft where a draft is due; `None` where a question is due, for
    `QuestionMatch` to grade (ticket #164)."""
    assert ctx.expected_output is not None
    if isinstance(ctx.expected_output, ExtractionQuestion):
        return None
    return isinstance(ctx.output, SessionExtraction)


@dataclass
class DateMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B15, ADR-0014): the expected absolute day and how it was found."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        drafts = _drafts(ctx)
        if drafts is None:
            return {}
        assert ctx.expected_output is not None
        return drafts and _occurred(ctx.output) == _occurred(ctx.expected_output)


@dataclass
class ActivitiesMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B1): the same activities, in any order, field for field, so a missing
    activity, a wrong count or an invented grade all fail. A case may list
    `metadata.ignore_fields` that the message leaves genuinely open."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        drafts = _drafts(ctx)
        if drafts is None:
            return {}
        assert ctx.expected_output is not None
        ignore = set((ctx.metadata or {}).get("ignore_fields", []))
        return drafts and _activity_keys(ctx.output, ignore=ignore) == _activity_keys(
            ctx.expected_output, ignore=ignore
        )


def _readings(output: BaseModel) -> list[str]:
    return sorted(
        json.dumps(reading.model_dump(mode="json"), sort_keys=True)
        for reading in getattr(output, "readings")  # noqa: B009
    )


@dataclass
class QuestionMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B17, ticket #164): a question exactly where one is due, never a guess and
    never a needless tap, and then the same readings in any order."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> bool:
        assert ctx.expected_output is not None
        due = isinstance(ctx.expected_output, ExtractionQuestion)
        if isinstance(ctx.output, ExtractionQuestion) != due:
            return False
        return not due or _readings(ctx.output) == _readings(ctx.expected_output)
