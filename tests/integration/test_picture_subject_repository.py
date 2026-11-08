"""What one exercise picture's run reads from the database (ADR-0031 *Picture generation*, #533):
the admin's active account by email, the catalogue exercise by exact name with its text, and
the image call's `llm_calls` row, which holds no prompt text."""

import base64
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID, uuid4

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.picture_subject_repository import SqlAlchemyPictureSubjectsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.exercises import CatalogueExercise, ExerciseMeasure, ExerciseText
from ai_trainer.domain.media_generation import AmbiguousExerciseName, PictureSubject
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.llm.images import OpenRouterImageGenerator

SessionFactory = Callable[[], AsyncSession]
_PROMPT = "One person hanging from a bar, then chest at the bar. Plain background. No text."


def _exercise(name: str, *texts: ExerciseText, sport_id: str = "gym") -> CatalogueExercise:
    return CatalogueExercise(
        sport_id=sport_id,
        source="zz-pic",
        source_ref=f"zz-pic-{uuid4()}",
        name=name,
        equipment="body weight",
        body_part="back",
        target="lats",
        secondary_muscles=(),
        default_measure=ExerciseMeasure.REPS,
        attribution="test",
        texts=texts,
    )


async def _user(factory: SessionFactory, email: str, status: UserStatus) -> User:
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(sub=f"sub-{uuid4()}", email=email, name=None, locale="en", status=status)
    )


async def _id(factory: SessionFactory, name: str) -> UUID:
    async with factory() as session:
        found = await session.scalar(
            text("SELECT id FROM exercises WHERE name = :name AND owner_user_id IS NULL"),
            {"name": name},
        )
    assert isinstance(found, UUID)
    return found


async def test_active_by_email_finds_an_active_user_case_insensitively(
    db_session_factory: SessionFactory,
) -> None:
    created = await _user(db_session_factory, "zz-pic-owner@example.com", UserStatus.ACTIVE)

    found = await SqlAlchemyUsersRepository(db_session_factory).active_by_email(
        "ZZ-Pic-Owner@Example.com"
    )

    assert found == created


async def test_active_by_email_ignores_a_pending_or_disabled_account(
    db_session_factory: SessionFactory,
) -> None:
    await _user(db_session_factory, "zz-pic-pending@example.com", UserStatus.PENDING)
    await _user(db_session_factory, "zz-pic-disabled@example.com", UserStatus.DISABLED)
    repository = SqlAlchemyUsersRepository(db_session_factory)

    assert await repository.active_by_email("zz-pic-pending@example.com") is None
    assert await repository.active_by_email("zz-pic-disabled@example.com") is None
    assert await repository.active_by_email("zz-pic-nobody@example.com") is None


async def test_catalogue_exercise_by_name_with_its_text_in_the_language_asked_first(
    db_session_factory: SessionFactory,
) -> None:
    english = ExerciseText(lang="en", instructions="Hang, then pull.", steps=("Hang.",))
    polish = ExerciseText(lang="pl", instructions="Zwis, potem podciągnij.", steps=())
    await SqlAlchemyExerciseRepository(db_session_factory).upsert_catalogue(
        [_exercise("Zz Pic Pull-Up", polish, english)]
    )
    subjects = SqlAlchemyPictureSubjectsRepository(db_session_factory)

    exercise_id = await _id(db_session_factory, "Zz Pic Pull-Up")

    assert await subjects.catalogue_exercise("zz pic PULL-UP", lang="en") == PictureSubject(
        exercise_id=exercise_id, name="Zz Pic Pull-Up", sport_id="gym", text=english
    )
    found = await subjects.catalogue_exercise("Zz Pic Pull-Up", lang="de")
    assert isinstance(found, PictureSubject)
    assert found.text == english


async def test_catalogue_exercise_with_no_text_or_no_match(
    db_session_factory: SessionFactory,
) -> None:
    await SqlAlchemyExerciseRepository(db_session_factory).upsert_catalogue(
        [_exercise("Zz Pic Bare", ExerciseText(lang="en", instructions="Gone.", steps=()))]
    )
    async with db_session_factory() as session:
        await session.execute(
            text(
                "DELETE FROM exercise_texts WHERE exercise_id IN "
                "(SELECT id FROM exercises WHERE name = 'Zz Pic Bare')"
            )
        )
        await session.commit()
    subjects = SqlAlchemyPictureSubjectsRepository(db_session_factory)

    assert await subjects.catalogue_exercise("Zz Pic Bare", lang="en") == PictureSubject(
        exercise_id=await _id(db_session_factory, "Zz Pic Bare"),
        name="Zz Pic Bare",
        sport_id="gym",
        text=None,
    )
    assert await subjects.catalogue_exercise("Zz Pic Nothing", lang="en") is None


async def test_a_name_in_more_than_one_sport_is_ambiguous(
    db_session_factory: SessionFactory,
) -> None:
    said = ExerciseText(lang="en", instructions="Hold.", steps=())
    await SqlAlchemyExerciseRepository(db_session_factory).upsert_catalogue(
        [
            _exercise("Zz Pic Plank", said, sport_id="gym"),
            _exercise("ZZ PIC PLANK", said, sport_id="climbing"),
        ]
    )

    found = await SqlAlchemyPictureSubjectsRepository(db_session_factory).catalogue_exercise(
        "zz pic plank", lang="en"
    )

    assert found == AmbiguousExerciseName(sport_ids=("climbing", "gym"))


async def test_a_name_twice_in_one_sport_is_not_ambiguous(
    db_session_factory: SessionFactory,
) -> None:
    said = ExerciseText(lang="en", instructions="Hold.", steps=())
    await SqlAlchemyExerciseRepository(db_session_factory).upsert_catalogue(
        [_exercise("Zz Pic Twice", said), _exercise("Zz Pic Twice", said)]
    )

    found = await SqlAlchemyPictureSubjectsRepository(db_session_factory).catalogue_exercise(
        "Zz Pic Twice", lang="en"
    )

    assert isinstance(found, PictureSubject)
    assert found.sport_id == "gym"


async def test_an_athletes_own_exercise_is_never_a_picture_subject(
    db_session_factory: SessionFactory,
) -> None:
    athlete = await _user(db_session_factory, "zz-pic-athlete@example.com", UserStatus.ACTIVE)
    await SqlAlchemyExerciseRepository(db_session_factory).create_custom(
        athlete.id, sport_id="gym", name="Zz Pic Mine", default_measure=ExerciseMeasure.REPS
    )

    found = await SqlAlchemyPictureSubjectsRepository(db_session_factory).catalogue_exercise(
        "Zz Pic Mine", lang="en"
    )

    assert found is None


async def test_success_row_in_the_database_holds_model_cost_and_template_without_prompt(
    db_session_factory: SessionFactory,
) -> None:
    """AC: the accounting row holds model, cost and template id, and no prompt text."""
    admin = await _user(db_session_factory, "zz-pic-admin@example.com", UserStatus.ACTIVE)
    reply = httpx2.Response(
        200,
        json={
            "data": [{"b64_json": base64.b64encode(b"jpeg").decode(), "media_type": "image/jpeg"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 1290, "cost": 0.0336},
        },
    )
    generator = OpenRouterImageGenerator(
        api_key="sk-or-v1-test",
        model="google/gemini-3.1-flash-lite-image",
        timeout_seconds=5,
        calls_repository=SqlAlchemyLlmCallsRepository(db_session_factory),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: reply)),
    )

    await generator.generate(user_id=admin.id, prompt=_PROMPT)

    async with db_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT * FROM llm_calls WHERE user_id = :id"), {"id": admin.id}
                )
            )
            .mappings()
            .all()
        )
    [row] = rows
    assert row["model"] == "google/gemini-3.1-flash-lite-image"
    assert row["cost"] == Decimal("0.0336")
    assert row["template_id"] == "media.picture"
    assert all(_PROMPT not in str(value) for value in row.values())
    assert all("Plain background" not in str(value) for value in row.values())
