"""Answer templates (`<sport>.answer`): a training question answered from the athlete's own saved
training, through the MCP tools (B40, ADR-0008, ADR-0010). Each plugin names its template, so a new
sport needs no edit here. The reply is text, so it streams (F1)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ai_trainer.domain.sports.base import AnswerSpec
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.llm.prompts.template import PromptTemplate
from ai_trainer.llm.router import ChatTurn, recent_turns


class AnswerDeps(BaseModel):
    """Every answer template's variables (ADR-0008): the message, the recent turns and the time
    context (ADR-0014). The athlete's identity is no variable: the host's hook sends it to the
    tools (ADR-0010)."""

    message: str
    history: list[ChatTurn] = []
    today: date
    now_local: datetime
    timezone: str


def build_answer_deps(
    *,
    message: str,
    history: list[ChatTurn],
    now: datetime,
    timezone: str,
    history_turns: int,
) -> AnswerDeps:
    """`now` comes from the `Clock` port; the local day is resolved in the athlete's timezone
    (ADR-0014)."""
    now_local = now.astimezone(ZoneInfo(timezone))
    return AnswerDeps(
        message=message,
        history=recent_turns(history, limit=history_turns),
        today=now_local.date(),
        now_local=now_local,
        timezone=timezone,
    )


def answer_template(spec: AnswerSpec) -> PromptTemplate[AnswerDeps, str]:
    return PromptTemplate(
        id=spec.template_id,
        version=spec.version,
        locale="en",
        deps_type=AnswerDeps,
        output_type=str,
        model_settings_key="answer_model",
        provider_settings_key="answer_provider_preferences",
        reasoning_effort_settings_key="answer_reasoning_effort",
    )


def answer_templates(sports: SportRegistry) -> list[PromptTemplate[AnswerDeps, str]]:
    """Each sport's answer template, in registry order."""
    return [answer_template(plugin.answers) for plugin in sports.all() if plugin.answers]
