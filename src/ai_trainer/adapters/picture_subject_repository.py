from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import ExerciseOrm, ExerciseTextOrm
from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.media_generation import AmbiguousExerciseName, PictureSubject


class SqlAlchemyPictureSubjectsRepository:
    """Implements `PictureSubjectsPort` (ADR-0031, #533): the shared catalogue only, so no
    athlete's exercise or text is ever drawn or sent to a model."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def catalogue_exercise(
        self, name: str, *, lang: str
    ) -> PictureSubject | AmbiguousExerciseName | None:
        async with self._session_factory() as session:
            matches = (
                await session.scalars(
                    select(ExerciseOrm)
                    .where(
                        ExerciseOrm.owner_user_id.is_(None),
                        func.lower(ExerciseOrm.name) == name.strip().lower(),
                    )
                    .order_by(ExerciseOrm.sport_id, ExerciseOrm.id)
                )
            ).all()
            if not matches:
                return None
            sport_ids = tuple(dict.fromkeys(match.sport_id for match in matches))
            if len(sport_ids) > 1:
                return AmbiguousExerciseName(sport_ids=sport_ids)
            exercise = matches[0]
            text = await session.scalar(
                select(ExerciseTextOrm)
                .where(ExerciseTextOrm.exercise_id == exercise.id)
                .order_by((ExerciseTextOrm.lang == lang).desc(), ExerciseTextOrm.lang)
                .limit(1)
            )
            return PictureSubject(
                exercise_id=exercise.id,
                name=exercise.name,
                sport_id=exercise.sport_id,
                text=None
                if text is None
                else ExerciseText(
                    lang=text.lang, instructions=text.instructions, steps=tuple(text.steps)
                ),
            )
