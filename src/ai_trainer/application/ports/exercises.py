from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.exercises import (
    CatalogueExercise,
    CatalogueWrite,
    Exercise,
    ExerciseAlias,
    ExerciseCandidate,
    ExerciseMeasure,
    ImportedCatalogue,
)


class ExerciseCatalogueSourcePort(Protocol):
    async def load(self) -> ImportedCatalogue:
        """Everything to import. Raises `ExerciseDatasetError` or `ExerciseCatalogueError`."""
        ...


class ExerciseNamesPort(Protocol):
    """What naming a typed exercise reads (ADR-0031). Every method is scoped to one `user_id`, and
    sees only the catalogue and that athlete's own exercises and aliases (invariant 3)."""

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        """The exercise each typed name is, by the athlete's own alias at `pg_trgm` similarity
        `near` or above, the most similar first. A name with none is left out."""
        ...

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        """At most `limit` exercises at `floor` or above, by trigram and `simple` full-text search
        over names and aliases in every language. Those measured like `measure` come first."""
        ...


class ExerciseRepositoryPort(Protocol):
    async def upsert_catalogue(self, exercises: Sequence[CatalogueExercise]) -> CatalogueWrite:
        """Writes the rows and their texts in one transaction, leaving matching rows unwritten.
        Never touches an athlete's custom exercises or aliases (ADR-0031)."""
        ...

    async def get(self, user_id: UUID, exercise_id: UUID) -> Exercise | None:
        """A catalogue exercise or `user_id`'s own; `None` for anyone else's."""
        ...

    async def create_custom(
        self, user_id: UUID, *, sport_id: str, name: str, default_measure: ExerciseMeasure
    ) -> Exercise: ...

    async def add_user_alias(
        self, user_id: UUID, exercise_id: UUID, alias: str
    ) -> ExerciseAlias | None:
        """Maps `alias` onto one exercise, replacing any earlier mapping of that name. `None` when
        `user_id` can't see the exercise."""
        ...

    async def user_aliases(self, user_id: UUID) -> Sequence[ExerciseAlias]: ...
