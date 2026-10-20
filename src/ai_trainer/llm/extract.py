"""Sport extraction templates (`<sport>.extract`): a log becomes a session draft (ADR-0006,
ADR-0008). Each plugin names its template and output model, so a new sport needs no edit here.
Dates come back absolute, with how they were found (ADR-0014)."""

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, field_validator
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from ai_trainer.domain.sports.base import ExtractionSpec
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.template import PromptTemplate
from ai_trainer.llm.router import ChatTurn, recent_turns


class ExtractDeps(BaseModel):
    """Every extraction template's variables (ADR-0008): the message, the recent turns, and
    the time context (ADR-0014). `weekday` is computed here, so "on Tuesday" never depends on
    the model knowing what day a date falls on. History keeps only the athlete's own turns: the
    assistant's are fixed lines with no draft content (ADR-0026 step 6). This deps model drops
    them itself, so an eval case's deps read like the app's."""

    message: str
    history: list[ChatTurn] = []
    today: date
    weekday: str
    now_local: datetime
    timezone: str

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
    )


def extract_template(
    spec: ExtractionSpec, *, version: int | None = None
) -> PromptTemplate[ExtractDeps, BaseModel]:
    """The plugin's template at its active version, or at an earlier `version` for evals."""
    return PromptTemplate(
        id=spec.template_id,
        version=spec.version if version is None else version,
        locale="en",
        deps_type=ExtractDeps,
        output_type=spec.output_type,
        model_settings_key="extract_model",
        provider_settings_key="extract_provider_preferences",
        reasoning_effort_settings_key="extract_reasoning_effort",
        # The app reads the output: the same log should give the same draft twice.
        temperature=0,
        output_retries=2,
        # It never writes to the athlete (ADR-0026 step 6).
        persona=False,
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


@dataclass
class DateMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B15, ADR-0014): the expected absolute day and how it was found."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> bool:
        assert ctx.expected_output is not None
        return _occurred(ctx.output) == _occurred(ctx.expected_output)


@dataclass
class ActivitiesMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B1): the same activities, in any order, field for field, so a missing
    activity, a wrong count or an invented grade all fail. A case may list
    `metadata.ignore_fields` that the message leaves genuinely open."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> bool:
        assert ctx.expected_output is not None
        ignore = set((ctx.metadata or {}).get("ignore_fields", []))
        return _activity_keys(ctx.output, ignore=ignore) == _activity_keys(
            ctx.expected_output, ignore=ignore
        )
