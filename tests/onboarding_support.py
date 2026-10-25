"""Fakes for the AI onboarding (ADR-0028, ticket #254): an in-memory `OnboardingAnswersPort` with
the database's revision rules, and an `OnboardingReaderPort` a test can hold mid-read."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.athlete_profile import AthleteProfile
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingAnswer, OnboardingStep
from ai_trainer.domain.sport_profile import SportProfile

type AnswerKey = tuple[UUID, OnboardingStep, str | None]


class FakeOnboardingAnswers:
    def __init__(self) -> None:
        self.sports: dict[UUID, list[str]] = {}
        self.timezones: dict[UUID, str] = {}
        self.answers: dict[AnswerKey, OnboardingAnswer] = {}
        self.profiles: dict[UUID, AthleteProfile] = {}
        self.sport_profiles: dict[UUID, dict[str, SportProfile]] = {}
        self.goals: dict[UUID, list[Goal]] = {}
        self.onboarded_at: dict[UUID, datetime] = {}

    async def start(self, user_id: UUID, sport_id: str, timezone: str) -> None:
        self.sports[user_id] = [sport_id]
        self.timezones[user_id] = timezone
        self.answers = {
            key: answer
            for key, answer in self.answers.items()
            if key[0] != user_id or key[2] in (None, sport_id)
        }

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

    async def discard_answers(self, user_id: UUID) -> None:
        self.answers = {key: a for key, a in self.answers.items() if key[0] != user_id}

    def _upsert(
        self,
        user_id: UUID,
        step: OnboardingStep,
        sport_id: str | None,
        text: str | None,
        status: AnswerStatus,
    ) -> int:
        earlier = self.answers.get((user_id, step, sport_id))
        revision = earlier.revision + 1 if earlier is not None else 1
        self.answers[(user_id, step, sport_id)] = OnboardingAnswer(
            step=step, sport_id=sport_id, text=text, revision=revision, status=status
        )
        return revision

    async def store_answer(
        self, user_id: UUID, step: OnboardingStep, sport_id: str | None, text: str
    ) -> int:
        return self._upsert(user_id, step, sport_id, text, AnswerStatus.PENDING)

    async def skip_answer(self, user_id: UUID, step: OnboardingStep, sport_id: str | None) -> int:
        return self._upsert(user_id, step, sport_id, None, AnswerStatus.SKIPPED)

    async def list_answers(self, user_id: UUID) -> Sequence[OnboardingAnswer]:
        order = list(OnboardingStep)
        return sorted(
            (answer for (owner, _, _), answer in self.answers.items() if owner == user_id),
            key=lambda answer: (order.index(answer.step), answer.sport_id or ""),
        )

    def _finish(
        self,
        key: AnswerKey,
        revision: int,
        from_statuses: tuple[AnswerStatus, ...],
        status: AnswerStatus,
        parsed: dict[str, JsonValue] | None,
    ) -> bool:
        answer = self.answers.get(key)
        if answer is None or answer.revision != revision or answer.status not in from_statuses:
            return False
        self.answers[key] = answer.model_copy(update={"status": status, "parsed": parsed})
        return True

    async def save_read(
        self,
        user_id: UUID,
        step: OnboardingStep,
        sport_id: str | None,
        revision: int,
        parsed: Mapping[str, JsonValue],
    ) -> bool:
        return self._finish(
            (user_id, step, sport_id),
            revision,
            (AnswerStatus.PENDING, AnswerStatus.FAILED),
            AnswerStatus.PARSED,
            dict(parsed),
        )

    async def mark_failed(
        self, user_id: UUID, step: OnboardingStep, sport_id: str | None, revision: int
    ) -> bool:
        return self._finish(
            (user_id, step, sport_id),
            revision,
            (AnswerStatus.PENDING,),
            AnswerStatus.FAILED,
            None,
        )

    async def confirm(
        self,
        user_id: UUID,
        *,
        profile: AthleteProfile,
        sport_ids: Sequence[str],
        sport_profiles: Mapping[str, SportProfile],
        goals: Sequence[Goal],
        timezone: str,
        when: datetime,
    ) -> None:
        self.profiles[user_id] = profile
        self.sport_profiles[user_id] = dict(sport_profiles)
        self.goals[user_id] = list(goals)
        self.sports[user_id] = list(sport_ids)
        self.timezones[user_id] = timezone
        self.onboarded_at[user_id] = when
        self.answers = {key: a for key, a in self.answers.items() if key[0] != user_id}


type Reply = dict[str, JsonValue] | None


class FakeReader:
    """Answers each read with `reply(text)`. While `hold` is set, a read waits for `release`, so
    a test can act while the model call is still in flight."""

    def __init__(self, reply: Callable[[str], Reply] | Reply = None) -> None:
        self._reply = reply
        self.hold = False
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[tuple[UUID, str | None, str, str]] = []

    async def read(
        self, user_id: UUID, answer: OnboardingAnswer, *, sport_id: str, timezone: str
    ) -> dict[str, JsonValue] | None:
        self.calls.append((user_id, answer.text, sport_id, timezone))
        self.started.set()
        if self.hold:
            await self.release.wait()
        if callable(self._reply):
            return self._reply(answer.text or "")
        return self._reply
