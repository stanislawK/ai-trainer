"""Importing the catalogue into the real Postgres (ADR-0031, ticket #375), from the recorded sample
over a mock transport. Each test rolls back, so a real import in the same database is untouched
and doesn't change these counts."""

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.domain.exercises import ExerciseMeasure
from ai_trainer.domain.users import NewUser, UserStatus
from tests.exercise_support import SAMPLE_CATALOGUE_DIR, SAMPLE_EXERCISES, sample_source

SessionFactory = Callable[[], AsyncSession]
_SAMPLE_ROWS = "source_ref LIKE 'zz-test-%'"


async def _scalar(factory: SessionFactory, statement: str) -> object:
    async with factory() as session:
        return (await session.execute(text(statement))).scalar_one()


async def _column(factory: SessionFactory, statement: str) -> list[object]:
    async with factory() as session:
        return list((await session.execute(text(statement))).scalars())


async def _versions(factory: SessionFactory) -> list[object]:
    """Every row's physical version, which any write to it changes, even to the same values."""
    return await _column(
        factory,
        "SELECT e.xmin::text || e.ctid::text || t.xmin::text || t.ctid::text "
        "FROM exercises e JOIN exercise_texts t ON t.exercise_id = e.id "
        f"WHERE e.{_SAMPLE_ROWS} ORDER BY e.source_ref, t.lang",
    )


async def test_fresh_import_loads_the_dataset_less_skips_plus_extra(
    db_session_factory: SessionFactory,
) -> None:
    repository = SqlAlchemyExerciseRepository(db_session_factory)

    report = await import_catalogue(source=sample_source(), exercises=repository)

    assert report.total == SAMPLE_EXERCISES
    assert report.skipped == 1
    assert report.write.inserted == SAMPLE_EXERCISES
    assert report.write.updated == 0
    assert (
        await _scalar(db_session_factory, f"SELECT count(*) FROM exercises WHERE {_SAMPLE_ROWS}")
        == 9
    )
    assert await _column(
        db_session_factory,
        "SELECT t.lang FROM exercise_texts t JOIN exercises e ON e.id = t.exercise_id "
        "WHERE e.source_ref = 'zz-test-01' ORDER BY t.lang",
    ) == ["en", "pl"]
    assert (
        await _scalar(
            db_session_factory,
            "SELECT count(*) FROM exercises "
            f"WHERE {_SAMPLE_ROWS} AND attribution LIKE '%Gym visual%'",
        )
        == 0
    )


async def test_second_import_changes_no_row(db_session_factory: SessionFactory) -> None:
    repository = SqlAlchemyExerciseRepository(db_session_factory)
    await import_catalogue(source=sample_source(), exercises=repository)
    before = await _versions(db_session_factory)

    report = await import_catalogue(source=sample_source(), exercises=repository)

    assert before
    assert await _versions(db_session_factory) == before
    assert report.write.inserted == 0
    assert report.write.updated == 0
    assert report.write.unchanged == SAMPLE_EXERCISES
    assert report.write.texts_written == 0


async def test_an_import_updates_what_changed_and_only_that(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    repository = SqlAlchemyExerciseRepository(db_session_factory)
    await import_catalogue(source=sample_source(), exercises=repository)
    (tmp_path / "extra.yaml").write_text((SAMPLE_CATALOGUE_DIR / "extra.yaml").read_text())
    (tmp_path / "overrides.yaml").write_text(
        (SAMPLE_CATALOGUE_DIR / "overrides.yaml").read_text().replace("Sled 45° Leg", "Sled Leg")
    )

    report = await import_catalogue(source=sample_source(directory=tmp_path), exercises=repository)

    assert report.write.updated == 1
    assert report.write.unchanged == SAMPLE_EXERCISES - 1
    assert (
        await _scalar(
            db_session_factory, "SELECT name FROM exercises WHERE source_ref = 'zz-test-04'"
        )
        == "Sled Leg Press"
    )


async def test_face_pull_exists_and_no_name_carries_a_gender_suffix(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    # The sample's overrides fit the sample; the committed supplement is what names Face Pull.
    repository = SqlAlchemyExerciseRepository(db_session_factory)
    (tmp_path / "overrides.yaml").write_text((SAMPLE_CATALOGUE_DIR / "overrides.yaml").read_text())
    (tmp_path / "extra.yaml").write_text(Path("data/catalogue/extra.yaml").read_text())

    await import_catalogue(source=sample_source(directory=tmp_path), exercises=repository)

    assert (
        await _scalar(db_session_factory, "SELECT source FROM exercises WHERE name = 'Face Pull'")
        == "ai-trainer"
    )
    assert (
        await _scalar(
            db_session_factory,
            "SELECT count(*) FROM exercises WHERE name ~* '\\((male|female)\\)'",
        )
        == 0
    )
    assert (
        await _scalar(
            db_session_factory, "SELECT name FROM exercises WHERE source_ref = 'zz-test-02'"
        )
        == "Astride Jumps"
    )


async def test_custom_exercise_and_user_alias_survive_a_reimport(
    db_session_factory: SessionFactory,
) -> None:
    repository = SqlAlchemyExerciseRepository(db_session_factory)
    await import_catalogue(source=sample_source(), exercises=repository)
    user = await SqlAlchemyUsersRepository(db_session_factory).create(
        NewUser(
            sub="reimport-user",
            email="reimport-user@example.com",
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    custom = await repository.create_custom(
        user.id, sport_id="gym", name="My Odd Row", default_measure=ExerciseMeasure.REPS
    )
    alias = await repository.add_user_alias(user.id, custom.id, "Rising straight arms")
    assert alias is not None

    await import_catalogue(source=sample_source(), exercises=repository)

    assert await repository.get(user.id, custom.id) == custom
    assert await repository.user_aliases(user.id) == [alias]
