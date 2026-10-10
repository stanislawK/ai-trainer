"""What the router reads in an athlete's message (ADR-0008)."""

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

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


class Intent(BaseModel):
    """One request in a message. `sport` is a `SportRegistry` ID, set only on `log_session`."""

    model_config = ConfigDict(frozen=True)

    kind: IntentKind
    sport: str | None = None
