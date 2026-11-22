"""`SimpleHowtoCheckPort` on Jev (ADR-0031 *Jev check*, ADR-0020, #591): the
`exercise_simple_howto_check` set asks whether a beginner summary keeps every safety and form cue
of the catalogue instructions it was written from, and whether everything it says is in them.
Both are probability questions phrased so that "yes" is good, each read at its own threshold
from `Settings`. The check fails closed."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.domain.exercise_simple_texts import HowtoCheck, HowtoCheckRefused
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decisions.loader import DecisionSet, DecisionSetLoader

SIMPLE_HOWTO_CHECK_ID = "exercise_simple_howto_check"
SIMPLE_HOWTO_CHECK_VERSION = 1
# Each must be at or above its own threshold for the summary to pass.
SIMPLE_HOWTO_CHECK_QUESTIONS = ("keeps_cues", "adds_nothing")


@dataclass(frozen=True, slots=True)
class HowtoCheckThresholds:
    """One threshold per question, named as the questions are. Two, because Jev scores them on
    different scales: it reads "every cue" strictly, so good summaries score low on `keeps_cues`
    while `adds_nothing` separates widely (calibration, 2026-11-22, ADR-0031)."""

    keeps_cues: float
    adds_nothing: float


class HowtoCheckState(BaseModel):
    """What Jev reads: the catalogue instructions and the summary, public text only. Extra
    fields are refused, so no identity can ride along (ADR-0020)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    original: str
    summary: str


def simple_howto_check_set(loader: DecisionSetLoader | None = None) -> DecisionSet:
    return (loader or DecisionSetLoader()).load(SIMPLE_HOWTO_CHECK_ID, SIMPLE_HOWTO_CHECK_VERSION)


def check_passes(output: BaseModel, *, thresholds: HowtoCheckThresholds) -> bool:
    """Whether every question's P(yes) is at or above its own threshold, read from the bounded
    float, never the library's 0.5 `bool` cut (ADR-0020). The smoke runner grades with this too."""
    return all(
        float(getattr(output, name)) >= getattr(thresholds, name)
        for name in SIMPLE_HOWTO_CHECK_QUESTIONS
    )


class JevSimpleHowtoCheck:
    """Implements `SimpleHowtoCheckPort` through the decision gateway, which writes the call's
    `llm_calls` row under the athlete and, in the app, counts it against their budget."""

    def __init__(
        self,
        decisions: DecisionGatewayPort,
        *,
        thresholds: HowtoCheckThresholds,
        loader: DecisionSetLoader | None = None,
    ) -> None:
        self._decisions = decisions
        self._thresholds = thresholds
        self._set = simple_howto_check_set(loader)

    async def check(
        self, *, user_id: UUID, original: str, summary: str
    ) -> HowtoCheck | HowtoCheckRefused:
        failed = HowtoCheck(passed=False, version=self._set.version)
        state = HowtoCheckState(original=original, summary=summary)
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
            return HowtoCheckRefused()
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return failed
        return HowtoCheck(
            passed=check_passes(result.output, thresholds=self._thresholds),
            version=self._set.version,
        )
