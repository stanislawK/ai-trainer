"""The beginner how-to on PostgreSQL (ADR-0031 *Beginner how-to*, #589): the source is the shared
catalogue's English text only; the first write wins and a second stores nothing; a stale row is
rewritten unless an admin edited it."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.exercise_simple_text_repository import (
    SqlAlchemyExerciseSimpleTextRepository,
)
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_simple_howto import WriteSimpleHowto
from ai_trainer.application.ports.llm_gateway import LlmGatewayResult
from ai_trainer.domain.exercise_simple_texts import (
    HowtoCallFailed,
    HowtoPresent,
    HowtoSkipped,
    HowtoSource,
    HowtoStored,
    NewSimpleText,
    NoHowto,
    SimpleHowto,
    SimpleHowtoDeps,
    SimpleHowtoDraft,
    SimpleTextStatus,
    StoredSimpleText,
    source_sha256,
)
from ai_trainer.domain.exercises import CatalogueExercise, ExerciseMeasure, ExerciseText
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.main import build_simple_howto
from ai_trainer.settings import Settings

SessionFactory = Callable[[], AsyncSession]
NOW = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
_OLD = "Lie flat on a bench and press the bar."
_NEW = "Lie flat on a bench with your feet flat. Lower the bar slowly, elbows tucked in."


class FrozenClock:
    def now(self) -> datetime:
        return NOW


@dataclass
class ScriptedWriter:
    """Implements `SimpleHowtoWriterPort`: the next text in `texts`, and the calls it got."""

    texts: list[str]
    calls: list[SimpleHowtoDeps] = field(default_factory=list)

    async def write(
        self, *, user_id: UUID, deps: SimpleHowtoDeps
    ) -> SimpleHowtoDraft | NoHowto | HowtoCallFailed:
        self.calls.append(deps)
        return SimpleHowtoDraft(
            text=self.texts.pop(0),
            template_id="exercise.simple_howto",
            template_version=1,
            model="test/model",
        )


def _exercise(name: str, *texts: ExerciseText) -> CatalogueExercise:
    return CatalogueExercise(
        sport_id="gym",
        source="zz-howto",
        source_ref=f"zz-howto-{uuid4()}",
        name=name,
        equipment="barbell",
        body_part="chest",
        target="pectorals",
        secondary_muscles=(),
        default_measure=ExerciseMeasure.LOAD_REPS,
        attribution="test",
        texts=texts,
    )


async def _catalogue(factory: SessionFactory, name: str, *texts: ExerciseText) -> UUID:
    await SqlAlchemyExerciseRepository(factory).upsert_catalogue([_exercise(name, *texts)])
    async with factory() as session:
        found = await session.scalar(
            text("SELECT id FROM exercises WHERE name = :name AND owner_user_id IS NULL"),
            {"name": name},
        )
    assert isinstance(found, UUID)
    return found


def _english(instructions: str) -> ExerciseText:
    return ExerciseText(lang="en", instructions=instructions, steps=())


def _new(exercise_id: UUID, body: str, instructions: str) -> NewSimpleText:
    return NewSimpleText(
        exercise_id=exercise_id,
        lang="en",
        text=body,
        template_id="exercise.simple_howto",
        template_version=1,
        model="test/model",
        source_sha256=source_sha256(instructions),
        created_at=NOW,
    )


def _service(factory: SessionFactory, writer: ScriptedWriter) -> WriteSimpleHowto:
    return WriteSimpleHowto(
        texts=SqlAlchemyExerciseSimpleTextRepository(factory),
        writer=writer,
        clock=FrozenClock(),
        max_chars=160,
    )


async def _row(factory: SessionFactory, exercise_id: UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT text, source_sha256, status, check_failures, flagged_at, edited_at "
                    "FROM exercise_simple_texts WHERE exercise_id = :id AND lang = 'en'"
                ),
                {"id": exercise_id},
            )
        ).one()
    return dict(row._mapping)


async def test_source_is_the_catalogue_exercise_with_its_english_text(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(
        db_session_factory,
        "Zz Howto Bench",
        ExerciseText(lang="pl", instructions="Połóż się.", steps=()),
        _english(_NEW),
    )

    found = await SqlAlchemyExerciseSimpleTextRepository(db_session_factory).howto_source(
        exercise, lang="en"
    )

    assert found == HowtoSource(
        exercise_id=exercise,
        name="Zz Howto Bench",
        instructions=_NEW,
        target="pectorals",
        equipment="barbell",
    )


# AC: a valid output is stored once; a second write stores nothing.
async def test_valid_howto_is_stored_once_and_a_second_write_stores_nothing(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Once", _english(_NEW))
    writer = ScriptedWriter(["Lie on the bench, feet flat. Lower the bar slowly, elbows tucked."])

    first = await _service(db_session_factory, writer).write(user_id=uuid4(), exercise_id=exercise)
    second = await _service(db_session_factory, writer).write(user_id=uuid4(), exercise_id=exercise)

    assert first == HowtoStored(
        text="Lie on the bench, feet flat. Lower the bar slowly, elbows tucked."
    )
    assert second == HowtoPresent()
    assert len(writer.calls) == 1
    row = await _row(db_session_factory, exercise)
    assert row["status"] == "shown"
    assert row["source_sha256"] == source_sha256(_NEW)
    assert row["check_failures"] == 0


async def test_insert_loses_to_a_concurrent_first_write(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Race", _english(_NEW))
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)

    assert await repository.write(_new(exercise, "First.", _NEW)) is True
    assert await repository.write(_new(exercise, "Second.", _NEW)) is False
    assert (await _row(db_session_factory, exercise))["text"] == "First."
    assert await repository.stored(exercise, lang="en") == StoredSimpleText(
        source_sha256=source_sha256(_NEW), status=SimpleTextStatus.SHOWN, edited_at=None
    )
    assert await repository.stored(exercise, lang="pl") is None


# AC: no English text, no call.
async def test_no_english_text_makes_no_call(db_session_factory: SessionFactory) -> None:
    polish_only = await _catalogue(
        db_session_factory,
        "Zz Howto Polish",
        ExerciseText(lang="pl", instructions="Połóż się na ławce.", steps=()),
    )
    owner = await SqlAlchemyUsersRepository(db_session_factory).create(
        NewUser(
            sub=f"sub-{uuid4()}",
            email="zz-howto@example.com",
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    custom = await SqlAlchemyExerciseRepository(db_session_factory).create_custom(
        owner.id, sport_id="gym", name="Zz My Press", default_measure=ExerciseMeasure.LOAD_REPS
    )
    async with db_session_factory() as session:
        # A custom exercise never has catalogue text; even with one, it gets no shared how-to.
        await session.execute(
            text(
                "INSERT INTO exercise_texts (exercise_id, lang, instructions, steps) "
                "VALUES (:id, 'en', 'Press it.', '{}')"
            ),
            {"id": custom.id},
        )
        await session.commit()
    writer = ScriptedWriter([])

    for exercise in (polish_only, custom.id, uuid4()):
        result = await _service(db_session_factory, writer).write(
            user_id=owner.id, exercise_id=exercise
        )
        assert result == HowtoSkipped(reason="no_english_text")
    assert writer.calls == []


# AC: a row with a stale `source_sha256` counts as missing, unless `edited_at` is set.
async def test_stale_source_is_rewritten(db_session_factory: SessionFactory) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Stale", _english(_NEW))
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)
    await repository.write(_new(exercise, "Old text.", _OLD))
    async with db_session_factory() as session:
        await session.execute(
            text(
                "UPDATE exercise_simple_texts SET status = 'flagged', flagged_at = :at, "
                "check_failures = 1 WHERE exercise_id = :id"
            ),
            {"id": exercise, "at": NOW},
        )
        await session.commit()
    writer = ScriptedWriter(["Lie on the bench, feet flat. Lower the bar slowly."])

    result = await _service(db_session_factory, writer).write(user_id=uuid4(), exercise_id=exercise)

    assert result == HowtoStored(text="Lie on the bench, feet flat. Lower the bar slowly.")
    assert await _row(db_session_factory, exercise) == {
        "text": "Lie on the bench, feet flat. Lower the bar slowly.",
        "source_sha256": source_sha256(_NEW),
        "status": "shown",
        "check_failures": 0,
        "flagged_at": None,
        "edited_at": None,
    }


async def test_stale_source_with_edited_at_is_kept(db_session_factory: SessionFactory) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Edited", _english(_NEW))
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)
    await repository.write(_new(exercise, "An admin's text.", _OLD))
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE exercise_simple_texts SET edited_at = :at WHERE exercise_id = :id"),
            {"id": exercise, "at": NOW},
        )
        await session.commit()
    writer = ScriptedWriter(["Never used."])

    result = await _service(db_session_factory, writer).write(user_id=uuid4(), exercise_id=exercise)

    assert result == HowtoPresent()
    assert writer.calls == []
    # Even a write sent anyway (a race with the edit) leaves the admin's text.
    assert await repository.write(_new(exercise, "Model text.", _NEW)) is False
    assert (await _row(db_session_factory, exercise))["text"] == "An admin's text."


@dataclass
class RecordingGateway:
    """Implements `LlmGatewayPort.run` only: remembers each call and returns `result`."""

    result: LlmGatewayResult[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run[OutputT: BaseModel](self, **kwargs: Any) -> LlmGatewayResult[OutputT]:
        self.calls.append(kwargs)
        return self.result

    def stream(self, **kwargs: Any) -> Any:  # pragma: no cover -- never streams
        raise AssertionError("the how-to never streams")


async def test_the_composition_root_writes_with_the_settings_template_and_gateway(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Wired", _english(_NEW))
    gateway = RecordingGateway(
        LlmGatewayResult(
            output=SimpleHowto(text="Lie on the bench, feet flat."),
            friendly_error=None,
            outcome=LlmCallOutcome.SUCCESS,
        )
    )
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("sk-or-v1-test"),
        eval_judge_model="test/judge",
        simple_howto_model="test/howto",
        exercise_simple_howto_timeout_seconds=3.0,
    )
    athlete = uuid4()

    result = await build_simple_howto(
        settings,
        db_session_factory,
        default_sport_registry(),
        gateway=gateway,
        clock=FrozenClock(),
    ).write(user_id=athlete, exercise_id=exercise)

    assert result == HowtoStored(text="Lie on the bench, feet flat.")
    [call] = gateway.calls
    assert (call["user_id"], call["template_id"], call["model_id"]) == (
        athlete,
        "exercise.simple_howto",
        "test/howto",
    )
    assert call["timeout_seconds"] == 3.0
    assert _NEW in call["instructions"]


async def test_a_deleted_exercise_takes_its_how_to_with_it(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Gone", _english(_NEW))
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)
    await repository.write(_new(exercise, "Text.", _NEW))
    async with db_session_factory() as session:
        await session.execute(text("DELETE FROM exercises WHERE id = :id"), {"id": exercise})
        await session.commit()

    assert await repository.stored(exercise, lang="en") is None
