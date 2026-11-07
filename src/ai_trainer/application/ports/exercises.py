from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.exercises import (
    CatalogueExercise,
    CatalogueWrite,
    Exercise,
    ExerciseAlias,
    ExerciseCandidate,
    ExerciseDetail,
    ExerciseEntry,
    ExerciseLabel,
    ExerciseMeasure,
    ImportedCatalogue,
    LoggedName,
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
        over names and aliases in every language. Those measured like `measure` come first. Each
        carries its short English text from `exercise_texts`, when it has one (F23, G5)."""
        ...

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> Mapping[str, ExerciseLabel]:
        """How the card names each typed name's exercise: its `name`, and `usual`, the athlete's
        own alias of that exercise at similarity `near` or above, when no alias is exactly the
        typed name. An exercise the athlete can't see is left out (ticket #402)."""
        ...


class ExerciseSearchPort(Protocol):
    """The catalogue's search by meaning, the shortlist's third input (ADR-0031, #429)."""

    async def similar(self, user_id: UUID, name: str) -> Sequence[UUID]:
        """Exercise IDs for a typed name, best first, each once, from the catalogue's texts. The
        embedding call is charged to `user_id`. Raises on a failed or empty-handed search."""
        ...


class ExerciseCandidatesPort(Protocol):
    async def candidates(
        self, user_id: UUID, exercise_ids: Sequence[UUID]
    ) -> Sequence[ExerciseCandidate]:
        """The exercises among `exercise_ids` that `user_id` can see, in the order given, each
        with its short English text. An ID the athlete can't see is left out (invariant 3)."""
        ...


class ExerciseLookupPort(ExerciseNamesPort, Protocol):
    """What `find_exercise` reads on top of naming (ADR-0010, ADR-0031)."""

    async def describe(
        self, user_id: UUID, exercise_ids: Sequence[UUID], *, lang: str
    ) -> Sequence[ExerciseDetail]:
        """The exercises among `exercise_ids` that `user_id` can see, in the order given, each
        with a short description from its text in `lang`, else in any language. An ID the
        athlete can't see is left out."""
        ...


class ExerciseHistoryPort(Protocol):
    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        """At most `limit` of `user_id`'s own entries of the exercise, the latest day first,
        with their sets in order. Another athlete's entries are never read."""
        ...

    async def typed_entries(
        self, user_id: UUID, typed_name: str, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        """As `entries`, for the typed-only entries whose normalized name is `typed_name`'s
        (ADR-0031): those with no exercise linked yet."""
        ...

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        """At most `limit` exercises `user_id` has logged that fit `name` at `near` or above,
        best first, by word similarity either way round against the name as typed and the
        exercise's own name (B40). Typed-only entries of one normalized name are one row."""
        ...


class ExercisePickerPort(Protocol):
    """Picks the exercise a typed name is out of a shortlist, for the draft the athlete still
    confirms (ADR-0031, ADR-0020). It never raises."""

    decision_id: str
    decision_version: int

    async def pick(
        self, user_id: UUID, name: str, candidates: Sequence[ExerciseCandidate]
    ) -> ExerciseCandidate | None:
        """The candidate when the pick is confident; `None` for a low-confidence pick, "none of
        these", a timeout, an error, and an empty shortlist, which makes no call."""
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
