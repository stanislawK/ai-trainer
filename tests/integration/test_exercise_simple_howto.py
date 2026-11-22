"""The beginner how-to on PostgreSQL (ADR-0031 *Beginner how-to*, #589, #591): the source is the
shared catalogue's English text only; the first write wins and a second stores nothing; a stale row
is rewritten unless an admin edited it. Jev's check runs before the write: a failed check stores no
text and counts, the second of the same source stores `rejected`, and every check writes an
`llm_calls` row under the athlete."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import models
from pydantic_ai.models.decision import (
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.exercise_simple_text_repository import (
    SqlAlchemyExerciseSimpleTextRepository,
)
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_simple_howto import WriteSimpleHowto
from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.application.ports.llm_gateway import LlmGatewayResult
from ai_trainer.domain.exercise_simple_texts import (
    CardHowtoSource,
    HowtoCallFailed,
    HowtoCheck,
    HowtoCheckRefused,
    HowtoFailed,
    HowtoPresent,
    HowtoRejected,
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
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.simple_howto_check import (
    SIMPLE_HOWTO_CHECK_ID,
    SIMPLE_HOWTO_CHECK_VERSION,
    HowtoCheckThresholds,
    JevSimpleHowtoCheck,
)
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
class ScriptedCheck:
    """Implements `SimpleHowtoCheckPort`: passes unless told to fail."""

    passed: bool = True
    calls: int = 0

    async def check(
        self, *, user_id: UUID, original: str, summary: str
    ) -> HowtoCheck | HowtoCheckRefused:
        self.calls += 1
        return HowtoCheck(passed=self.passed, version=SIMPLE_HOWTO_CHECK_VERSION)


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
        check_version=SIMPLE_HOWTO_CHECK_VERSION,
        created_at=NOW,
    )


def _service(
    factory: SessionFactory, writer: ScriptedWriter, check: ScriptedCheck | None = None
) -> WriteSimpleHowto:
    return WriteSimpleHowto(
        texts=SqlAlchemyExerciseSimpleTextRepository(factory),
        writer=writer,
        check=check or ScriptedCheck(),
        clock=FrozenClock(),
        max_chars=160,
    )


async def _row(factory: SessionFactory, exercise_id: UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT text, source_sha256, status, check_version, check_failures, "
                    "flagged_at, edited_at "
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
        source_sha256=source_sha256(_NEW),
        status=SimpleTextStatus.SHOWN,
        edited_at=None,
        has_text=True,
        check_failures=0,
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
        "check_version": SIMPLE_HOWTO_CHECK_VERSION,
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


@dataclass
class PassingDecisions:
    """Implements `DecisionGatewayPort`: every question at P(yes) 1, and the calls it got."""

    calls: list[tuple[UUID, str, int]] = field(default_factory=list)

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        self.calls.append((user_id, decision_id, decision_version))
        output = output_type.model_validate(dict.fromkeys(output_type.model_fields, 1.0))
        return DecisionResult(output=output, outcome=LlmCallOutcome.SUCCESS)


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

    decisions = PassingDecisions()

    result = await build_simple_howto(
        settings,
        db_session_factory,
        default_sport_registry(),
        gateway=gateway,
        decisions=decisions,
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
    assert decisions.calls == [(athlete, SIMPLE_HOWTO_CHECK_ID, SIMPLE_HOWTO_CHECK_VERSION)]


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


# --- Jev's check (#591) --------------------------------------------------------------------


async def _athlete(factory: SessionFactory, label: str) -> UUID:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=f"sub-{uuid4()}",
            email=f"zz-howto-{label}@example.com",
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    return user.id


# AC 1: a passing summary is stored `shown`.
async def test_write_after_passing_check_stores_shown_with_check_version(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Passed", _english(_NEW))
    check = ScriptedCheck()

    result = await _service(
        db_session_factory, ScriptedWriter(["Lower the bar slowly."]), check
    ).write(user_id=uuid4(), exercise_id=exercise)

    assert result == HowtoStored(text="Lower the bar slowly.")
    assert check.calls == 1
    row = await _row(db_session_factory, exercise)
    assert (row["status"], row["text"], row["check_version"], row["check_failures"]) == (
        "shown",
        "Lower the bar slowly.",
        SIMPLE_HOWTO_CHECK_VERSION,
        0,
    )


# AC 2: a second failure stores `rejected`; a later write makes no call.
async def test_second_failed_check_of_same_source_stores_rejected(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Rejected", _english(_NEW))
    writer = ScriptedWriter(["First try.", "Second try.", "Never used."])
    check = ScriptedCheck(passed=False)

    first = await _service(db_session_factory, writer, check).write(
        user_id=uuid4(), exercise_id=exercise
    )
    once = await _row(db_session_factory, exercise)
    second = await _service(db_session_factory, writer, check).write(
        user_id=uuid4(), exercise_id=exercise
    )
    third = await _service(db_session_factory, writer, check).write(
        user_id=uuid4(), exercise_id=exercise
    )

    assert first == HowtoFailed(reason="check_failed")
    assert (once["text"], once["status"], once["check_failures"]) == (None, "shown", 1)
    assert second == third == HowtoRejected()
    assert len(writer.calls) == check.calls == 2
    row = await _row(db_session_factory, exercise)
    assert (row["text"], row["status"], row["check_version"], row["check_failures"]) == (
        "Second try.",
        "rejected",
        SIMPLE_HOWTO_CHECK_VERSION,
        2,
    )
    assert await SqlAlchemyExerciseSimpleTextRepository(db_session_factory).stored(
        exercise, lang="en"
    ) == StoredSimpleText(
        source_sha256=source_sha256(_NEW),
        status=SimpleTextStatus.REJECTED,
        edited_at=None,
        has_text=True,
        check_failures=2,
    )


async def test_a_once_failed_row_takes_a_passing_text_and_keeps_its_count(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Retry", _english(_NEW))
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)

    assert await repository.record_failed_check(_new(exercise, "Bad.", _NEW)) == "counted"
    assert await repository.write(_new(exercise, "Good.", _NEW)) is True

    row = await _row(db_session_factory, exercise)
    assert (row["text"], row["status"], row["check_failures"]) == ("Good.", "shown", 1)


async def test_failed_check_of_new_source_restarts_count(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Restart", _english(_NEW))
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)
    await repository.record_failed_check(_new(exercise, "Old one.", _OLD))
    assert await repository.record_failed_check(_new(exercise, "Old two.", _OLD)) == "rejected"

    assert await repository.record_failed_check(_new(exercise, "New one.", _NEW)) == "counted"

    row = await _row(db_session_factory, exercise)
    assert (row["text"], row["status"], row["source_sha256"], row["check_failures"]) == (
        None,
        "shown",
        source_sha256(_NEW),
        1,
    )


async def test_failed_check_never_overwrites_a_row_with_text(
    db_session_factory: SessionFactory,
) -> None:
    """Another athlete's write passed first, or an admin edited the row: a failed check of the
    same source leaves it as it is."""
    passed = await _catalogue(db_session_factory, "Zz Howto Kept", _english(_NEW))
    edited = await _catalogue(db_session_factory, "Zz Howto Kept Edit", _english(_NEW))
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)
    await repository.write(_new(passed, "Passed.", _NEW))
    await repository.write(_new(edited, "An admin's text.", _OLD))
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE exercise_simple_texts SET edited_at = :at WHERE exercise_id = :id"),
            {"id": edited, "at": NOW},
        )
        await session.commit()

    assert await repository.record_failed_check(_new(passed, "Failed.", _NEW)) == "present"
    assert await repository.record_failed_check(_new(edited, "Failed.", _NEW)) == "present"

    for exercise, kept in ((passed, "Passed."), (edited, "An admin's text.")):
        row = await _row(db_session_factory, exercise)
        assert (row["text"], row["status"], row["check_failures"]) == (kept, "shown", 0)


async def test_a_row_without_text_must_be_a_shown_failed_check(
    db_session_factory: SessionFactory,
) -> None:
    exercise = await _catalogue(db_session_factory, "Zz Howto Constraint", _english(_NEW))
    async with db_session_factory() as session:
        with pytest.raises(Exception, match="exercise_simple_texts_text_or_failed_check"):
            await session.execute(
                text(
                    "INSERT INTO exercise_simple_texts (exercise_id, lang, text, template_id, "
                    "template_version, model, source_sha256, status, check_failures, created_at) "
                    "VALUES (:id, 'en', NULL, 't', 1, 'm', :sha, 'shown', 0, :at)"
                ),
                {"id": exercise, "sha": source_sha256(_NEW), "at": NOW},
            )


class _Jev(DecisionModel[None]):
    """Jev behind the real gateway, no network: both questions at P(yes) `probability`."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, probability: float) -> None:
        super().__init__()
        self._probability = probability
        self.requests: list[DecisionRequest] = []

    @property
    def model_name(self) -> str:
        return "typesafe/jev-1.13"

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        self.requests.append(request)
        answers: dict[str, DecisionAnswer] = {
            name: NoulAnswer(noul=self._probability) for name in request.questions
        }
        return DecisionResponse(
            answers=answers, model_name=self.model_name, usage=RequestUsage(input_tokens=150)
        )


def _refuse(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"a test reached the network: {request.url}")


# AC 5: each check writes an `llm_calls` row for the athlete.
async def test_check_writes_one_llm_calls_row_for_the_athlete(
    db_session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    exercise = await _catalogue(db_session_factory, "Zz Howto Accounted", _english(_NEW))
    athlete = await _athlete(db_session_factory, "accounted")
    calls = SqlAlchemyLlmCallsRepository(db_session_factory)
    jev = _Jev(0.95)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse)) as client:
        gateway = OpenRouterDecisionGateway(Settings(), calls, http_client=client)
        check = JevSimpleHowtoCheck(
            gateway, thresholds=HowtoCheckThresholds(keeps_cues=0.12, adds_nothing=0.5)
        )
        service = WriteSimpleHowto(
            texts=SqlAlchemyExerciseSimpleTextRepository(db_session_factory),
            writer=ScriptedWriter(["Lower the bar slowly, elbows tucked."]),
            check=check,
            clock=FrozenClock(),
            max_chars=160,
        )
        with gateway.agent.override(model=jev):
            result = await service.write(user_id=athlete, exercise_id=exercise)

    assert result == HowtoStored(text="Lower the bar slowly, elbows tucked.")
    [request] = jev.requests
    assert set(request.questions) == {"keeps_cues", "adds_nothing"}
    [row] = await calls.list_for_user(athlete)
    assert (row.user_id, row.template_id, row.template_version) == (
        athlete,
        SIMPLE_HOWTO_CHECK_ID,
        SIMPLE_HOWTO_CHECK_VERSION,
    )
    assert row.outcome is LlmCallOutcome.SUCCESS
    assert row.cost is None


# The card reads every candidate's source and stored short version in one query (#592).
async def test_card_rows_reads_catalogue_sources_with_their_stored_text(
    db_session_factory: SessionFactory,
) -> None:
    written = await _catalogue(db_session_factory, "Zz Howto Card Written", _english(_NEW))
    fresh = await _catalogue(db_session_factory, "Zz Howto Card Fresh", _english(_OLD))
    polish_only = await _catalogue(
        db_session_factory,
        "Zz Howto Card Polish",
        ExerciseText(lang="pl", instructions="Połóż się na ławce.", steps=()),
    )
    owner = await SqlAlchemyUsersRepository(db_session_factory).create(
        NewUser(
            sub=f"sub-{uuid4()}",
            email="zz-howto-card@example.com",
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    custom = await SqlAlchemyExerciseRepository(db_session_factory).create_custom(
        owner.id, sport_id="gym", name="Zz My Card Press", default_measure=ExerciseMeasure.LOAD_REPS
    )
    async with db_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO exercise_texts (exercise_id, lang, instructions, steps) "
                "VALUES (:id, 'en', 'Press it.', '{}')"
            ),
            {"id": custom.id},
        )
        await session.commit()
    repository = SqlAlchemyExerciseSimpleTextRepository(db_session_factory)
    await repository.write(_new(written, "Lie down, lower the bar.", _NEW))

    found = await repository.card_rows([written, fresh, polish_only, custom.id, uuid4()], lang="en")

    assert found == {
        written: CardHowtoSource(
            instructions=_NEW,
            stored=StoredSimpleText(
                source_sha256=source_sha256(_NEW),
                status=SimpleTextStatus.SHOWN,
                edited_at=None,
                has_text=True,
                check_failures=0,
            ),
            text="Lie down, lower the bar.",
        ),
        fresh: CardHowtoSource(instructions=_OLD, stored=None, text=None),
    }
    assert await repository.card_rows([], lang="en") == {}
