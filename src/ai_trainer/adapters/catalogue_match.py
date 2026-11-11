"""Shortlisting an outside source's exercise name against the shared catalogue (ADR-0031's #552
and #555 amendments): plain trigram similarity over each catalogue exercise's name and catalogue
aliases, never an athlete's own exercise or alias, for Jev to pick from. The athletes'
shortlist's full-text term is left out on purpose: it lifts any name holding the typed words to
its floor, so every such name would tie and crowd the shortlist in alphabetical order."""

from collections.abc import Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseAliasOrm, ExerciseOrm
from ai_trainer.domain.exercises import ExerciseCandidate


class SqlAlchemyCatalogueMatch:
    """Implements `CatalogueExerciseMatchPort`. It reads only catalogue rows, so it takes no
    `user_id`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def catalogue_shortlist(
        self, *, sport_id: str, name: str, floor: float, limit: int
    ) -> Sequence[ExerciseCandidate]:
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
            rows = await session.execute(
                select(
                    ExerciseOrm.id,
                    ExerciseOrm.name,
                    ExerciseOrm.equipment,
                    ExerciseOrm.target,
                    score,
                )
                .where(
                    ExerciseOrm.sport_id == sport_id,
                    ExerciseOrm.owner_user_id.is_(None),
                    score >= floor,
                )
                .order_by(score.desc(), ExerciseOrm.name, ExerciseOrm.id)
                .limit(limit)
            )
            return [
                ExerciseCandidate(
                    id=id_, name=found, equipment=equipment, target=target, name_score=float(fit)
                )
                for id_, found, equipment, target, fit in rows.tuples()
            ]
