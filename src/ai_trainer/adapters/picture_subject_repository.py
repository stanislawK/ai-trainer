from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseOrm, ExerciseTextOrm
from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.media_generation import PictureSubject


class SqlAlchemyPictureSubjectsRepository:
    """Implements `PictureSubjectsPort` (ADR-0031, #533): the shared catalogue only, so no
    athlete's exercise or text is ever drawn or sent to a model."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def catalogue_exercise(self, name: str, *, lang: str) -> PictureSubject | None:
        async with self._session_factory() as session:
            exercise = await session.scalar(
                select(ExerciseOrm)
                .where(
                    ExerciseOrm.owner_user_id.is_(None),
                    func.lower(ExerciseOrm.name) == name.strip().lower(),
                )
                .order_by(ExerciseOrm.sport_id, ExerciseOrm.id)
                .limit(1)
            )
            if exercise is None:
                return None
            text = await session.scalar(
                select(ExerciseTextOrm)
                .where(ExerciseTextOrm.exercise_id == exercise.id)
                .order_by((ExerciseTextOrm.lang == lang).desc(), ExerciseTextOrm.lang)
                .limit(1)
            )
            return PictureSubject(
                name=exercise.name,
                sport_id=exercise.sport_id,
                text=None
                if text is None
                else ExerciseText(
                    lang=text.lang, instructions=text.instructions, steps=tuple(text.steps)
                ),
            )
