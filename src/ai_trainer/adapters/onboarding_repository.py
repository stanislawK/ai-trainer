from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import (
    AthleteProfileOrm,
    GoalOrm,
    OnboardingAnswerOrm,
    UserOrm,
    UserSportOrm,
    WeeklyAvailabilityOrm,
)
from ai_trainer.domain.athlete_profile import AthleteProfile
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingAnswer, OnboardingStep

_ANSWER_KEY = "onboarding_answers_user_step_sport_key"
_STEP_ORDER = [step.value for step in OnboardingStep]


class SqlAlchemyOnboardingRepository:
    """Implements `OnboardingRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        async with self._session_factory() as session:
            if general_goals_of:
                await session.execute(
                    update(GoalOrm)
                    .where(GoalOrm.user_id == user_id, GoalOrm.sport_id.in_(general_goals_of))
                    .values(sport_id=None)
                )
            if delete_goals_of:
                await session.execute(
                    delete(GoalOrm).where(
                        GoalOrm.user_id == user_id, GoalOrm.sport_id.in_(delete_goals_of)
                    )
                )
            await _replace_sports(session, user_id, sport_ids)
            await session.commit()

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(UserSportOrm.sport_id)
                .where(UserSportOrm.user_id == user_id)
                .order_by(UserSportOrm.created_at, UserSportOrm.sport_id)
            )
            return list(rows)

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(WeeklyAvailabilityOrm).where(WeeklyAvailabilityOrm.user_id == user_id)
            )
            if minutes_by_weekday:
                # `ON CONFLICT DO NOTHING`, as in `replace_sports`: a double-tapped Continue.
                await session.execute(
                    insert(WeeklyAvailabilityOrm)
                    .values(
                        [
                            {"user_id": user_id, "weekday": weekday, "minutes": minutes}
                            for weekday, minutes in minutes_by_weekday.items()
                        ]
                    )
                    .on_conflict_do_nothing()
                )
            await session.commit()

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(WeeklyAvailabilityOrm.weekday, WeeklyAvailabilityOrm.minutes).where(
                    WeeklyAvailabilityOrm.user_id == user_id
                )
            )
            return {weekday: minutes for weekday, minutes in rows}

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        async with self._session_factory() as session:
            await session.execute(delete(GoalOrm).where(GoalOrm.user_id == user_id))
            if goals:
                # `ON CONFLICT DO NOTHING` on (user_id, position), as in `replace_sports`: a
                # double-tapped Continue must neither 500 nor store every goal twice.
                await session.execute(
                    insert(GoalOrm)
                    .values(
                        [
                            {
                                "user_id": user_id,
                                "position": position,
                                "text": goal.text,
                                "target_date": goal.target_date,
                                "sport_id": goal.sport_id,
                            }
                            for position, goal in enumerate(goals)
                        ]
                    )
                    .on_conflict_do_nothing()
                )
            await session.commit()

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(GoalOrm.text, GoalOrm.target_date, GoalOrm.sport_id)
                .where(GoalOrm.user_id == user_id)
                .order_by(GoalOrm.position)
            )
            return [
                Goal(text=text, target_date=target_date, sport_id=sport_id)
                for text, target_date, sport_id in rows
            ]

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(UserOrm).where(UserOrm.id == user_id).values(onboarded_at=when)
            )
            await session.commit()

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(UserOrm)
                .where(UserOrm.id == user_id)
                .values(timezone=timezone, onboarded_at=when)
            )
            await session.commit()

    async def discard_answers(self, user_id: UUID) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(OnboardingAnswerOrm).where(OnboardingAnswerOrm.user_id == user_id)
            )
            await session.commit()

    async def start(self, user_id: UUID, sport_id: str, timezone: str) -> None:
        async with self._session_factory() as session:
            await _replace_sports(session, user_id, [sport_id])
            await session.execute(
                delete(OnboardingAnswerOrm).where(
                    OnboardingAnswerOrm.user_id == user_id,
                    OnboardingAnswerOrm.sport_id.is_not(None),
                    OnboardingAnswerOrm.sport_id != sport_id,
                )
            )
            await session.execute(
                update(UserOrm).where(UserOrm.id == user_id).values(timezone=timezone)
            )
            await session.commit()

    async def store_answer(
        self, user_id: UUID, step: OnboardingStep, sport_id: str | None, text: str
    ) -> int:
        return await self._upsert_answer(user_id, step, sport_id, text, AnswerStatus.PENDING)

    async def skip_answer(self, user_id: UUID, step: OnboardingStep, sport_id: str | None) -> int:
        return await self._upsert_answer(user_id, step, sport_id, None, AnswerStatus.SKIPPED)

    async def _upsert_answer(
        self,
        user_id: UUID,
        step: OnboardingStep,
        sport_id: str | None,
        text: str | None,
        status: AnswerStatus,
    ) -> int:
        values = insert(OnboardingAnswerOrm).values(
            user_id=user_id,
            step=step.value,
            sport_id=sport_id,
            text=text,
            revision=1,
            status=status.value,
            parsed=None,
        )
        statement = values.on_conflict_do_update(
            constraint=_ANSWER_KEY,
            set_={
                "text": values.excluded.text,
                "status": values.excluded.status,
                "parsed": None,
                "revision": OnboardingAnswerOrm.revision + 1,
                "updated_at": func.now(),
            },
        ).returning(OnboardingAnswerOrm.revision)
        async with self._session_factory() as session:
            revision = int((await session.execute(statement)).scalar_one())
            await session.commit()
            return revision

    async def list_answers(self, user_id: UUID) -> Sequence[OnboardingAnswer]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(OnboardingAnswerOrm)
                .where(OnboardingAnswerOrm.user_id == user_id)
                .order_by(
                    func.array_position(_STEP_ORDER, OnboardingAnswerOrm.step),
                    OnboardingAnswerOrm.sport_id,
                )
            )
            return [
                OnboardingAnswer(
                    step=OnboardingStep(row.step),
                    sport_id=row.sport_id,
                    text=row.text,
                    revision=row.revision,
                    status=AnswerStatus(row.status),
                    parsed=row.parsed,
                )
                for row in rows
            ]

    async def save_read(
        self,
        user_id: UUID,
        step: OnboardingStep,
        sport_id: str | None,
        revision: int,
        parsed: Mapping[str, JsonValue],
    ) -> bool:
        return await self._finish_read(
            user_id,
            step,
            sport_id,
            revision,
            from_statuses=(AnswerStatus.PENDING, AnswerStatus.FAILED),
            status=AnswerStatus.PARSED,
            parsed=dict(parsed),
        )

    async def mark_failed(
        self, user_id: UUID, step: OnboardingStep, sport_id: str | None, revision: int
    ) -> bool:
        return await self._finish_read(
            user_id,
            step,
            sport_id,
            revision,
            from_statuses=(AnswerStatus.PENDING,),
            status=AnswerStatus.FAILED,
            parsed=None,
        )

    async def _finish_read(
        self,
        user_id: UUID,
        step: OnboardingStep,
        sport_id: str | None,
        revision: int,
        *,
        from_statuses: Sequence[AnswerStatus],
        status: AnswerStatus,
        parsed: dict[str, JsonValue] | None,
    ) -> bool:
        # The revision check is the write's own condition, so a stale read can never land, even
        # when a newer send commits between the read starting and finishing (invariant 3).
        async with self._session_factory() as session:
            result = await session.execute(
                update(OnboardingAnswerOrm)
                .where(
                    _answer_is(user_id, step, sport_id),
                    OnboardingAnswerOrm.revision == revision,
                    OnboardingAnswerOrm.status.in_([value.value for value in from_statuses]),
                )
                .values(status=status.value, parsed=parsed, updated_at=func.now())
                .returning(OnboardingAnswerOrm.id)
            )
            saved = result.first() is not None
            await session.commit()
            return saved

    async def get_profile(self, user_id: UUID) -> AthleteProfile | None:
        async with self._session_factory() as session:
            row = await session.get(AthleteProfileOrm, user_id)
            if row is None:
                return None
            return AthleteProfile(
                birth_year=row.birth_year,
                height_cm=row.height_cm,
                weight_kg=float(row.weight_kg) if row.weight_kg is not None else None,
                limits=row.limits,
            )

    async def confirm(
        self,
        user_id: UUID,
        *,
        profile: AthleteProfile,
        sport_ids: Sequence[str],
        timezone: str,
        when: datetime,
    ) -> None:
        values = {
            "birth_year": profile.birth_year,
            "height_cm": profile.height_cm,
            "weight_kg": Decimal(str(profile.weight_kg)) if profile.weight_kg is not None else None,
            "limits": profile.limits,
        }
        async with self._session_factory() as session:
            upsert = insert(AthleteProfileOrm).values(user_id=user_id, **values)
            await session.execute(
                upsert.on_conflict_do_update(index_elements=["user_id"], set_=values)
            )
            await _replace_sports(session, user_id, sport_ids)
            await session.execute(
                update(UserOrm)
                .where(UserOrm.id == user_id)
                .values(timezone=timezone, onboarded_at=when)
            )
            await session.execute(
                delete(OnboardingAnswerOrm).where(OnboardingAnswerOrm.user_id == user_id)
            )
            await session.commit()


def _answer_is(user_id: UUID, step: OnboardingStep, sport_id: str | None) -> ColumnElement[bool]:
    return (
        (OnboardingAnswerOrm.user_id == user_id)
        & (OnboardingAnswerOrm.step == step.value)
        & OnboardingAnswerOrm.sport_id.is_not_distinct_from(sport_id)
    )


async def _replace_sports(session: AsyncSession, user_id: UUID, sport_ids: Sequence[str]) -> None:
    """Makes `sport_ids` exactly the user's `user_sports` rows, inside the caller's
    transaction."""
    await session.execute(delete(UserSportOrm).where(UserSportOrm.user_id == user_id))
    if sport_ids:
        # `ON CONFLICT DO NOTHING`: a double-tapped Continue runs two of these at once, and both
        # may delete before either inserts; the loser must not 500.
        await session.execute(
            insert(UserSportOrm)
            .values([{"user_id": user_id, "sport_id": sport_id} for sport_id in sport_ids])
            .on_conflict_do_nothing()
        )
