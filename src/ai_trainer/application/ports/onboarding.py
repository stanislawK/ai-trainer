from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.athlete_profile import AthleteProfile
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.onboarding import OnboardingAnswer, OnboardingStep


class OnboardingRepositoryPort(Protocol):
    """Persists what onboarding collects about the athlete (ADR-0006). Every method is
    scoped to one `user_id`, which comes from the authenticated session (ADR-0005)."""

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        """Makes `sport_ids` exactly the user's `user_sports` rows. In the same transaction,
        the user's goals of the sports in `general_goals_of` lose their sport and the goals
        of those in `delete_goals_of` are deleted; every other goal is untouched."""
        ...

    async def list_sports(self, user_id: UUID) -> Sequence[str]: ...

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        """Makes `minutes_by_weekday` (weekday 0 = Monday to minutes) exactly the user's
        `weekly_availability` rows."""
        ...

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]: ...

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        """Makes `goals` exactly the user's `goals` rows, keeping their order."""
        ...

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]: ...

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None: ...

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        """Stores the user's IANA `timezone` and sets `onboarded_at` to `when`, together."""
        ...


class OnboardingAnswersPort(Protocol):
    """Persists the AI onboarding's answers and confirms the summary (ADR-0028). Every method is
    scoped to one `user_id`, which comes from the authenticated session (ADR-0005)."""

    async def start(self, user_id: UUID, sport_id: str, timezone: str) -> None:
        """Makes `sport_id` the user's only `user_sports` row, drops answers of any other sport,
        and stores the browser's IANA `timezone`, together. The athlete's own pick, not a read,
        so it is written before the summary (ADR-0028: flow state comes from the server)."""
        ...

    async def list_sports(self, user_id: UUID) -> Sequence[str]: ...

    async def discard_answers(self, user_id: UUID) -> None:
        """Deletes the user's answers, so onboarding starts over (a fresh dev-session user)."""
        ...

    async def store_answer(
        self, user_id: UUID, step: OnboardingStep, sport_id: str | None, text: str
    ) -> int:
        """Stores `text` as the step's answer, `pending`, clearing any earlier read, and
        returns its new revision."""
        ...

    async def skip_answer(self, user_id: UUID, step: OnboardingStep, sport_id: str | None) -> int:
        """Stores the step as `skipped`, with no text, and returns its new revision."""
        ...

    async def list_answers(self, user_id: UUID) -> Sequence[OnboardingAnswer]:
        """The user's answers in step order."""
        ...

    async def save_read(
        self,
        user_id: UUID,
        step: OnboardingStep,
        sport_id: str | None,
        revision: int,
        parsed: Mapping[str, JsonValue],
    ) -> bool:
        """Stores what a read produced, only while the answer is still at `revision` and not
        skipped; returns whether it landed (ADR-0028 invariant 3)."""
        ...

    async def mark_failed(
        self, user_id: UUID, step: OnboardingStep, sport_id: str | None, revision: int
    ) -> bool:
        """Marks a `pending` answer still at `revision` as `failed`; returns whether it did."""
        ...

    async def confirm(
        self,
        user_id: UUID,
        *,
        profile: AthleteProfile,
        sport_ids: Sequence[str],
        timezone: str,
        when: datetime,
    ) -> None:
        """In one transaction: stores `profile`, makes `sport_ids` the user's sports, stores the
        timezone, sets `onboarded_at` to `when`, and deletes every answer (ADR-0028 invariants
        1 and 4)."""
        ...


class OnboardingReaderPort(Protocol):
    """Reads one onboarding answer into its step's fields with that step's template
    (ADR-0028). Writes the call's `llm_calls` row under `user_id`."""

    async def read(
        self, user_id: UUID, answer: OnboardingAnswer, *, sport_id: str, timezone: str
    ) -> dict[str, JsonValue] | None:
        """The fields read, in the step's own shape, or `None` when the call failed. A field the
        answer leaves unclear is absent or null, never guessed. `sport_id` is the sport the
        athlete is onboarding with."""
        ...
