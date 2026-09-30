from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import GoalOrm, UserOrm, UserSportOrm, WeeklyAvailabilityOrm
from ai_trainer.domain.goals import Goal


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
            await session.execute(delete(UserSportOrm).where(UserSportOrm.user_id == user_id))
            if sport_ids:
                # `ON CONFLICT DO NOTHING`: a double-tapped Continue runs two of these at once,
                # and both may delete before either inserts; the loser must not 500.
                await session.execute(
                    insert(UserSportOrm)
                    .values([{"user_id": user_id, "sport_id": sport_id} for sport_id in sport_ids])
                    .on_conflict_do_nothing()
                )
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
