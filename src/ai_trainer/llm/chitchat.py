"""The `chitchat` template: the persona's reply to chitchat, unclear and wellbeing messages
(ADR-0008, skeleton routing)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ai_trainer.domain.goals import Goal
from ai_trainer.llm.prompts.template import PromptTemplate
from ai_trainer.llm.router import ChatTurn, recent_turns

CHITCHAT_TEMPLATE_ID = "chitchat"


class ChitchatDeps(BaseModel):
    """The chitchat template's variables (ADR-0008): the message, the recent turns, the
    athlete's sports and goals, and the time context (ADR-0014)."""

    message: str
    history: list[ChatTurn] = []
    athlete_sports: list[str] = []
    goals: list[Goal] = []
    today: date
    now_local: datetime
    timezone: str


def build_chitchat_deps(
    *,
    message: str,
    history: list[ChatTurn],
    athlete_sports: list[str],
    goals: list[Goal],
    now: datetime,
    timezone: str,
    history_turns: int,
) -> ChitchatDeps:
    """`now` comes from the `Clock` port; `today` and `now_local` are resolved in the
    athlete's timezone (ADR-0014)."""
    now_local = now.astimezone(ZoneInfo(timezone))
    return ChitchatDeps(
        message=message,
        history=recent_turns(history, limit=history_turns),
        athlete_sports=athlete_sports,
        goals=goals,
        today=now_local.date(),
        now_local=now_local,
        timezone=timezone,
    )


def chitchat_template() -> PromptTemplate[ChitchatDeps, str]:
    return PromptTemplate(
        id=CHITCHAT_TEMPLATE_ID,
        version=1,
        locale="en",
        deps_type=ChitchatDeps,
        output_type=str,
        model_settings_key="chitchat_model",
    )
