"""Matching an outside source's exercise name against the shared catalogue (ADR-0031's #552
amendment): plain trigram similarity over each catalogue exercise's name and catalogue aliases,
never an athlete's own exercise or alias. The shortlist's full-text term is left out on purpose:
it lifts any name holding the typed words to its floor, so at a 0.75 threshold every such name
would tie and the pick would fall to alphabetical order."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseAliasOrm, ExerciseOrm


class SqlAlchemyCatalogueMatch:
    """Implements `CatalogueExerciseMatchPort`. It reads only catalogue rows, so it takes no
    `user_id`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def best_catalogue_match(
        self, *, sport_id: str, name: str, threshold: float
    ) -> UUID | None:
        typed = func.exercise_name_norm(name)
        by_alias = (
            select(func.max(func.similarity(ExerciseAliasOrm.alias_norm, typed)))
            .where(
                ExerciseAliasOrm.exercise_id == ExerciseOrm.id,
                ExerciseAliasOrm.user_id.is_(None),
            )
            .scalar_subquery()
        )
        score = func.greatest(
            func.similarity(func.exercise_name_norm(ExerciseOrm.name), typed),
            func.coalesce(by_alias, 0.0),
        )
        async with self._session_factory() as session:
            found: UUID | None = await session.scalar(
                select(ExerciseOrm.id)
                .where(
                    ExerciseOrm.sport_id == sport_id,
                    ExerciseOrm.owner_user_id.is_(None),
                    score >= threshold,
                )
                .order_by(score.desc(), ExerciseOrm.name, ExerciseOrm.id)
                .limit(1)
            )
        return found
