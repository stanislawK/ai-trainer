"""`PictureBriefCheckPort` on Jev (ADR-0031 *Picture on the card*, ADR-0020, #642): the
`picture_brief_check` set asks whether a picture brief shows the same movement as the catalogue
instructions it was written from, and whether it keeps the brief rules (one figure, start and end
position, plain background, no printed text). Jev takes no image input, so it checks the brief,
not the picture. Both are probability questions phrased so that "yes" is good, each read at its
own threshold from `Settings`. The check fails closed."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.media_generation import BriefCheck, BriefCheckRefused
from ai_trainer.llm.decisions.loader import DecisionSet, DecisionSetLoader

PICTURE_BRIEF_CHECK_ID = "picture_brief_check"
PICTURE_BRIEF_CHECK_VERSION = 1
# Each must be at or above its own threshold for the brief to pass.
PICTURE_BRIEF_CHECK_QUESTIONS = ("same_movement", "keeps_rules")


@dataclass(frozen=True, slots=True)
class BriefCheckThresholds:
    """One threshold per question, named as the questions are."""

    same_movement: float
    keeps_rules: float


class BriefCheckState(BaseModel):
    """What Jev reads: the catalogue instructions and the brief, public text only. Extra fields
    are refused, so no identity can ride along (ADR-0020)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instructions: str
    brief: str


def picture_brief_check_set(loader: DecisionSetLoader | None = None) -> DecisionSet:
    return (loader or DecisionSetLoader()).load(PICTURE_BRIEF_CHECK_ID, PICTURE_BRIEF_CHECK_VERSION)


def check_passes(output: BaseModel, *, thresholds: BriefCheckThresholds) -> bool:
    """Whether every question's P(yes) is at or above its own threshold, read from the bounded
    float, never the library's 0.5 `bool` cut (ADR-0020). The smoke runner grades with this too."""
    return all(
        float(getattr(output, name)) >= getattr(thresholds, name)
        for name in PICTURE_BRIEF_CHECK_QUESTIONS
    )


class JevPictureBriefCheck:
    """Implements `PictureBriefCheckPort` through the decision gateway, which writes the call's
    `llm_calls` row under the acting user."""

    def __init__(
        self,
        decisions: DecisionGatewayPort,
        *,
        thresholds: BriefCheckThresholds,
        loader: DecisionSetLoader | None = None,
    ) -> None:
        self._decisions = decisions
        self._thresholds = thresholds
        self._set = picture_brief_check_set(loader)

    async def check(
        self, *, user_id: UUID, instructions: str, brief: str
    ) -> BriefCheck | BriefCheckRefused:
        failed = BriefCheck(passed=False, version=self._set.version)
        state = BriefCheckState(instructions=instructions, brief=brief)
        try:
            result = await self._decisions.decide(
                user_id=user_id,
                decision_id=self._set.id,
                decision_version=self._set.version,
                output_type=self._set.output_type,
                state=state.model_dump_json(),
            )
        except Exception:
            # The gateway maps model failures to outcomes; this catches the rest (its row write
            # failing, say). The check fails closed.
            return failed
        if result.outcome is LlmCallOutcome.RATE_LIMITED:
            return BriefCheckRefused()
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return failed
        return BriefCheck(
            passed=check_passes(result.output, thresholds=self._thresholds),
            version=self._set.version,
        )
