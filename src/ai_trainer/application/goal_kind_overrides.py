"""The goal kinds an athlete changed from Jev's pick on the onboarding summary (PRD 0013 B45,
ADR-0034, ticket #628): counted by position, and recorded as content-free `decision_overridden`
events."""

from uuid import UUID

from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.ports.draft_settling import DecisionRef
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.sport_profile import SportForm, SportSection, typed_goal_kind


def overridden_kinds(read: SportSection, typed: SportForm) -> int:
    """How many of the goals Jev read now carry another kind than its pick, by position; a goal
    the athlete added on the summary has no pick to override, and one they cleared is dropped, so
    it has no kind left to compare."""
    kinds, texts = typed.goal_kinds, typed.goal_texts
    return sum(
        goal.kind != typed_goal_kind(kinds[n] if n < len(kinds) else "")
        for n, goal in enumerate(read.goals)
        if n < len(texts) and texts[n].strip()
    )


async def record_overridden_kinds(
    feedback: FeedbackRecorder, decision: DecisionRef, user_id: UUID, count: int
) -> None:
    """One event per changed goal, naming the decision and the field, never a goal or a kind."""
    for _ in range(count):
        await feedback.record(
            user_id,
            FeedbackKind.DECISION_OVERRIDDEN,
            template_id=decision.decision_id,
            template_version=decision.version,
            fields=decision.fields,
        )
