"""Settling gym names on PostgreSQL (PRD-0010 B37, ADR-0031, ADR-0032, ticket #397): the whole path
from a log through its question to the confirm, on the real repositories and the sample catalogue.
An alias, a custom exercise and a link are written only by the confirm."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clarification_repository import SqlAlchemyClarificationRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.reply import (
    ReplyDone,
    ReplyServices,
    SessionSettled,
    abandon_clarification,
    answer_clarification,
    start_reply,
)
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.clarification import KEEP_NAME_OPTION, StoredClarification
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseMeasure
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.settings import Settings
from tests.activity_support import STORES
from tests.chat_support import (
    InMemoryChatRepository,
    InMemoryProfile,
    ScriptedConversation,
    reply_services,
)
from tests.exercise_support import sample_source
from tests.integration.test_session_continuity_repository import (
    _START,
    _Athlete,
    _athlete,
    _Clock,
)
from tests.picker_support import ScriptedPicker

SessionFactory = Callable[[], AsyncSession]
_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)


def _lift(name: str = "bench press") -> dict[str, Any]:
    return {
        "name": name,
        "sets": 3,
        "reps": 8,
        "load": {"original": "60kg", "value": 60, "unit": "kg"},
    }


def _extraction(*names: str) -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [_lift(name) for name in names],
        }
    )


class _Logger:
    """One athlete logging through the reply, on the real draft, question and session stores."""

    def __init__(
        self, factory: SessionFactory, athlete: _Athlete, picker: ScriptedPicker | None = None
    ) -> None:
        self.factory = factory
        self.athlete = athlete
        self.picker = picker or ScriptedPicker()
        self.user_id = athlete.user_id
        self.exercises = SqlAlchemyExerciseRepository(factory)
        self.conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]]
        )
        self.drafts = SqlAlchemySessionDraftRepository(factory, stores=STORES)
        self.chat = SqlAlchemyChatRepository(factory)
        scripted = reply_services(
            InMemoryChatRepository(),
            profile=InMemoryProfile({self.user_id: ["gym"]}),
            conversation=self.conversation,
            settlers={
                "gym": ResolveExercises(
                    self.exercises,
                    self.picker,
                    question=self.conversation.exercise_question,
                    limits=_LIMITS,
                )
            },
        )
        self.services: ReplyServices = replace(
            scripted,
            chat=self.chat,
            conversations=SqlAlchemyConversationRepository(
                factory, clock=_Clock(), retention_days=14
            ),
            drafts=self.drafts,
            clarifications=SqlAlchemyClarificationRepository(factory),
            sessions=SqlAlchemyTrainingSessionRepository(factory, stores=STORES),
        )
        self._logs = 0

    async def log(self, *names: str) -> tuple[StoredDraft, tuple[StoredClarification, ...]]:
        self.conversation.extractions = [_extraction(*names)]
        self._logs += 1
        # Minutes apart, so a log joins the session before it and no same-session question asks.
        message = await self.chat.add(
            self.user_id,
            self.athlete.conversation_id,
            ChatRole.USER,
            " ".join(names),
            _START + timedelta(minutes=self._logs),
        )
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        *_, done = [event async for event in events]
        assert isinstance(done, ReplyDone), done
        [draft] = done.drafts
        return draft, tuple(done.clarifications)

    async def answer(self, question: StoredClarification, option_id: str) -> StoredDraft:
        settled = await answer_clarification(
            self.user_id,
            question.id,
            [option_id],
            timezone="Europe/Warsaw",
            services=self.services,
        )
        assert isinstance(settled, SessionSettled)
        return settled.draft

    async def confirm(self, draft: StoredDraft) -> StoredDraft:
        return await self.drafts.confirm(self.user_id, draft.id, confirmed_at=_later(draft))


def _later(draft: StoredDraft) -> datetime:
    return draft.created_at + timedelta(seconds=1)


async def _logger(
    factory: SessionFactory, sub: str, picker: ScriptedPicker | None = None
) -> _Logger:
    await import_catalogue(source=sample_source(), exercises=SqlAlchemyExerciseRepository(factory))
    return _Logger(factory, await _athlete(factory, sub), picker)


class _PicksBarbell(ScriptedPicker):
    """Jev, confident of "Barbell Bench Press" whenever the real shortlist offers it."""

    async def pick(
        self, user_id: UUID, name: str, candidates: Sequence[ExerciseCandidate]
    ) -> ExerciseCandidate | None:
        await super().pick(user_id, name, candidates)
        return next((c for c in candidates if c.name == "Barbell Bench Press"), None)


def _option(question: StoredClarification, name: str) -> str:
    return next(
        option.id
        for option in question.clarification.options
        if option.params.get("name") == name and option.id != KEEP_NAME_OPTION
    )


async def _custom_rows(factory: SessionFactory, user_id: UUID) -> list[str]:
    async with factory() as db:
        rows = await db.scalars(
            text("SELECT name FROM exercises WHERE owner_user_id = :id"), {"id": user_id}
        )
        return list(rows)


async def _entry_ids(factory: SessionFactory, user_id: UUID) -> list[UUID | None]:
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT e.exercise_id FROM gym_exercise_entries e"
                " WHERE e.user_id = :id ORDER BY e.session_id, e.position"
            ),
            {"id": user_id},
        )
        return [row[0] for row in rows]


async def test_confirmed_name_resolves_with_no_question(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-known")
    draft, [question] = await logger.log("bench press")
    settled = await logger.answer(question, _option(question, "Barbell Bench Press"))
    await logger.confirm(settled)

    again, asked = await logger.log("Bench  Press")

    assert asked == ()
    [exercise] = GymPayload.model_validate(again.draft.payload).activities
    assert exercise.exercise_id == UUID(_option(question, "Barbell Bench Press"))
    assert exercise.name_as_typed == "Bench  Press"
    assert draft.id != again.id


async def test_picking_a_candidate_and_confirming_stores_one_alias(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-pick")

    _, [question] = await logger.log("bench press")
    names = [option.params.get("name") for option in question.clarification.options]
    assert "Barbell Bench Press" in names
    assert question.clarification.options[-1].id == KEEP_NAME_OPTION
    picked = _option(question, "Barbell Bench Press")

    settled = await logger.answer(question, picked)
    assert await logger.exercises.user_aliases(logger.user_id) == []

    await logger.confirm(settled)

    [alias] = await logger.exercises.user_aliases(logger.user_id)
    assert alias.alias == "bench press"
    assert alias.exercise_id == UUID(picked)
    assert await _entry_ids(db_session_factory, logger.user_id) == [UUID(picked)]


async def test_a_jev_pick_stores_its_alias_only_at_confirm(
    db_session_factory: SessionFactory,
) -> None:
    picker = _PicksBarbell()
    logger = await _logger(db_session_factory, "resolve-jev", picker)

    draft, asked = await logger.log("bench press")

    assert asked == ()
    [exercise] = GymPayload.model_validate(draft.draft.payload).activities
    assert exercise.exercise_id is not None
    assert exercise.jev_pick
    assert [name for _, name, _ in picker.asked] == ["bench press"]
    # Jev's pick is on the draft and nowhere else.
    assert await logger.exercises.user_aliases(logger.user_id) == []
    assert await _entry_ids(db_session_factory, logger.user_id) == []

    await logger.confirm(draft)

    [alias] = await logger.exercises.user_aliases(logger.user_id)
    assert (alias.alias, alias.exercise_id) == ("bench press", exercise.exercise_id)
    assert await _entry_ids(db_session_factory, logger.user_id) == [exercise.exercise_id]
    # From now on history answers, with no second call to Jev.
    again, asked_again = await logger.log("Bench  Press")
    assert asked_again == ()
    assert len(picker.asked) == 1
    [repeat] = GymPayload.model_validate(again.draft.payload).activities
    assert repeat.exercise_id == exercise.exercise_id
    assert not repeat.jev_pick


async def test_discarding_a_draft_jev_filled_stores_nothing(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-jev-discard", _PicksBarbell())

    draft, _ = await logger.log("bench press")
    await logger.drafts.discard(logger.user_id, draft.id, discarded_at=_later(draft))

    assert await logger.exercises.user_aliases(logger.user_id) == []
    assert await _custom_rows(db_session_factory, logger.user_id) == []
    assert await _entry_ids(db_session_factory, logger.user_id) == []


async def test_keep_my_name_creates_a_custom_exercise_only_that_athlete_sees(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-keep")
    other = await _logger(db_session_factory, "resolve-keep-other")

    _, [question] = await logger.log("rising straight arms")
    settled = await logger.answer(question, KEEP_NAME_OPTION)
    assert await _custom_rows(db_session_factory, logger.user_id) == []

    await logger.confirm(settled)

    assert await _custom_rows(db_session_factory, logger.user_id) == ["rising straight arms"]
    [alias] = await logger.exercises.user_aliases(logger.user_id)
    [entry] = await _entry_ids(db_session_factory, logger.user_id)
    assert entry == alias.exercise_id
    custom = await logger.exercises.get(logger.user_id, alias.exercise_id)
    assert custom is not None
    assert custom.owner_user_id == logger.user_id
    assert custom.default_measure == ExerciseMeasure.LOAD_REPS
    assert await logger.exercises.get(other.user_id, alias.exercise_id) is None

    _, [theirs] = await other.log("rising straight arms")
    assert str(alias.exercise_id) not in [option.id for option in theirs.clarification.options]

    again, asked = await logger.log("rising straight arms")
    assert asked == ()
    [exercise] = GymPayload.model_validate(again.draft.payload).activities
    assert exercise.exercise_id == alias.exercise_id


async def test_a_custom_exercise_is_a_candidate_for_a_similar_name_of_its_owner(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-own-candidate")
    custom = await logger.exercises.create_custom(
        logger.user_id,
        sport_id="gym",
        name="Zottman Curl Variation",
        default_measure=ExerciseMeasure.LOAD_REPS,
    )

    _, [question] = await logger.log("zottman curl")

    assert str(custom.id) in [option.id for option in question.clarification.options]


async def test_discarding_the_draft_stores_no_alias_or_custom_exercise(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-discard")

    _, [picked] = await logger.log("bench press")
    first = await logger.answer(picked, _option(picked, "Barbell Bench Press"))
    await logger.drafts.discard(logger.user_id, first.id, discarded_at=_later(first))
    _, [kept] = await logger.log("rising straight arms")
    second = await logger.answer(kept, KEEP_NAME_OPTION)
    await logger.drafts.discard(logger.user_id, second.id, discarded_at=_later(second))

    assert await logger.exercises.user_aliases(logger.user_id) == []
    assert await _custom_rows(db_session_factory, logger.user_id) == []


async def test_no_candidate_above_the_floor_offers_only_keep_my_name_on_the_real_search(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-nothing")

    _, [question] = await logger.log("xyzzy qwerty")

    assert [option.id for option in question.clarification.options] == [KEEP_NAME_OPTION]
    assert question.clarification.allow_free_text


async def test_let_me_type_instead_leaves_a_typed_only_entry_and_asks_again_next_time(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-type")

    _, [question] = await logger.log("bench press")
    settled = await abandon_clarification(logger.user_id, question.id, services=logger.services)
    assert isinstance(settled, SessionSettled)
    await logger.confirm(settled.draft)

    assert await _entry_ids(db_session_factory, logger.user_id) == [None]
    assert await logger.exercises.user_aliases(logger.user_id) == []
    _, asked = await logger.log("bench press")
    assert len(asked) == 1


async def test_a_question_writes_nothing_but_itself(db_session_factory: SessionFactory) -> None:
    """ADR-0015 invariant 5's integration test for this question."""
    logger = await _logger(db_session_factory, "resolve-nothing-written")

    draft, _ = await logger.log("bench press")

    assert draft.status == "pending"
    assert draft.session_id is None
    assert await logger.exercises.user_aliases(logger.user_id) == []
    assert await _custom_rows(db_session_factory, logger.user_id) == []
    assert await _entry_ids(db_session_factory, logger.user_id) == []


async def test_a_second_athletes_alias_is_never_matched(
    db_session_factory: SessionFactory,
) -> None:
    owner = await _logger(db_session_factory, "resolve-tenant-owner")
    other = await _logger(db_session_factory, "resolve-tenant-other")
    _, [question] = await owner.log("bench press")
    await owner.confirm(await owner.answer(question, _option(question, "Barbell Bench Press")))

    _, asked = await other.log("bench press")

    assert len(asked) == 1


async def test_a_near_match_counts_and_its_confirm_stores_the_new_wording(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-near")
    _, [question] = await logger.log("bench press")
    picked = _option(question, "Barbell Bench Press")
    await logger.confirm(await logger.answer(question, picked))

    near, asked = await logger.log("bench pres")

    assert asked == ()
    [exercise] = GymPayload.model_validate(near.draft.payload).activities
    assert exercise.exercise_id == UUID(picked)
    await logger.confirm(near)
    assert sorted(alias.alias for alias in await logger.exercises.user_aliases(logger.user_id)) == [
        "bench pres",
        "bench press",
    ]


async def test_a_different_exercise_of_a_similar_name_is_not_a_near_match(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-not-near")
    _, [question] = await logger.log("incline bench press")
    await logger.confirm(await logger.answer(question, KEEP_NAME_OPTION))

    _, asked = await logger.log("decline bench press")

    assert len(asked) == 1


async def test_a_forged_exercise_id_in_a_draft_stores_no_alias(
    db_session_factory: SessionFactory,
) -> None:
    owner = await _logger(db_session_factory, "resolve-forge-owner")
    forger = await _logger(db_session_factory, "resolve-forge-forger")
    custom = await owner.exercises.create_custom(
        owner.user_id, sport_id="gym", name="Owner Row", default_measure=ExerciseMeasure.REPS
    )
    payload = _extraction("stolen row").to_payload()
    payload.activities[0].exercise_id = custom.id
    draft = await forger.athlete.draft(
        "stolen row",
        _START,
        SessionDraft.model_validate(
            {
                "occurred_on": "2026-10-02",
                "date_source": "assumed_today",
                "timezone": "Europe/Warsaw",
                "payload": payload.model_dump(mode="json"),
            }
        ),
        sport_id="gym",
    )

    await forger.confirm(draft)

    assert await forger.exercises.user_aliases(forger.user_id) == []
    assert await _entry_ids(db_session_factory, forger.user_id) == [None]


async def test_undo_keeps_the_alias_and_the_custom_exercise(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-undo")
    _, [question] = await logger.log("rising straight arms")
    confirmed = await logger.confirm(await logger.answer(question, KEEP_NAME_OPTION))

    await logger.drafts.undo(logger.user_id, confirmed.id, undone_at=_later(confirmed))

    assert await _entry_ids(db_session_factory, logger.user_id) == []
    assert await _custom_rows(db_session_factory, logger.user_id) == ["rising straight arms"]
    assert len(await logger.exercises.user_aliases(logger.user_id)) == 1


async def test_two_confirms_at_once_leave_one_custom_exercise() -> None:
    """On their own connections, committed for real: the shared test connection can't race, so
    this test cleans up by deleting its user (CASCADE, G7)."""
    engine = build_engine(str(Settings().database_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: UUID | None = None
    try:
        logger = _Logger(factory, await _athlete(factory, f"resolve-race-{uuid4()}"))
        user_id = logger.user_id
        _, [first_question] = await logger.log("rising straight arms")
        first = await logger.answer(first_question, KEEP_NAME_OPTION)
        _, [second_question] = await logger.log("rising straight arms")
        second = await logger.answer(second_question, KEEP_NAME_OPTION)

        await asyncio.gather(logger.confirm(first), logger.confirm(second))

        assert await _custom_rows(factory, user_id) == ["rising straight arms"]
        [alias] = await logger.exercises.user_aliases(user_id)
        assert await _entry_ids(factory, user_id) == [alias.exercise_id, alias.exercise_id]
    finally:
        if user_id is not None:
            async with factory() as db:
                await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
                await db.commit()
        await engine.dispose()


async def test_keep_my_name_never_links_to_another_sports_exercise_of_that_name(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-other-sport")
    elsewhere = await logger.exercises.create_custom(
        logger.user_id, sport_id="climbing", name="Hangboard", default_measure=ExerciseMeasure.TIME
    )
    await logger.exercises.add_user_alias(logger.user_id, elsewhere.id, "zzz hangboard")

    _, [question] = await logger.log("zzz hangboard")
    await logger.confirm(await logger.answer(question, KEEP_NAME_OPTION))

    [entry] = await _entry_ids(db_session_factory, logger.user_id)
    assert entry is not None
    assert entry != elsewhere.id
    assert sorted(await _custom_rows(db_session_factory, logger.user_id)) == [
        "Hangboard",
        "zzz hangboard",
    ]


async def _alias_version(factory: SessionFactory, user_id: UUID) -> tuple[Any, ...]:
    async with factory() as db:
        row = (
            await db.execute(
                text("SELECT id, xmin::text FROM exercise_aliases WHERE user_id = :id"),
                {"id": user_id},
            )
        ).one()
        return tuple(row)


async def test_a_routine_log_of_a_known_name_rewrites_no_alias(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "resolve-routine")
    _, [question] = await logger.log("bench press")
    await logger.confirm(await logger.answer(question, _option(question, "Barbell Bench Press")))
    before = await _alias_version(db_session_factory, logger.user_id)

    again, _ = await logger.log("bench press")
    await logger.confirm(again)

    assert await _alias_version(db_session_factory, logger.user_id) == before


async def _search(
    logger: _Logger, name: str, *, measure: str = "load_reps", limit: int = 4
) -> list[str]:
    found = await logger.exercises.shortlist(
        logger.user_id,
        sport_id="gym",
        name=name,
        measure=measure,
        floor=_LIMITS.candidate_floor,
        limit=limit,
    )
    return [candidate.name for candidate in found]


async def test_the_shortlist_finds_a_name_by_its_words_and_stays_within_its_limit(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "shortlist-words")

    assert "Barbell Bench Press" in await _search(logger, "bench press")
    assert len(await _search(logger, "bench press", limit=1)) == 1
    assert await _search(logger, "xyzzy qwerty") == []


async def test_the_shortlist_finds_an_exercise_by_a_global_alias_in_any_language(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "shortlist-alias")
    exercise = await logger.exercises.create_custom(
        logger.user_id, sport_id="gym", name="Qqq", default_measure=ExerciseMeasure.REPS
    )
    async with db_session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO exercise_aliases (id, exercise_id, alias)"
                " VALUES (gen_random_uuid(), :id, 'Wyciskanie na ławce')"
            ),
            {"id": exercise.id},
        )
        await db.commit()

    assert await _search(logger, "wyciskanie na lawce") == ["Qqq"]


async def test_the_shortlist_puts_the_logs_own_measure_first(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "shortlist-measure")
    for name, measure in (
        ("Zzz Press", ExerciseMeasure.BAND_REPS),
        ("Zzz Press Machine", ExerciseMeasure.LOAD_REPS),
    ):
        await logger.exercises.create_custom(
            logger.user_id, sport_id="gym", name=name, default_measure=measure
        )

    async def order(measure: str) -> list[str]:
        found = await _search(logger, "zzz press", measure=measure, limit=100)
        return [name for name in found if name.startswith("Zzz")]

    assert await order("load_reps") == ["Zzz Press Machine", "Zzz Press"]
    assert await order("band_reps") == ["Zzz Press", "Zzz Press Machine"]


async def test_the_shortlist_never_offers_another_athletes_exercise_or_alias(
    db_session_factory: SessionFactory,
) -> None:
    owner = await _logger(db_session_factory, "shortlist-owner")
    other = await _logger(db_session_factory, "shortlist-other")
    custom = await owner.exercises.create_custom(
        owner.user_id, sport_id="gym", name="Zzz Secret", default_measure=ExerciseMeasure.REPS
    )
    await owner.exercises.add_user_alias(owner.user_id, custom.id, "yyy hidden")
    barbell = await _catalogue_id(db_session_factory, "zz-test-01")
    await owner.exercises.add_user_alias(owner.user_id, barbell, "qqq private")

    assert await _search(owner, "zzz secret") == ["Zzz Secret"]
    assert await _search(other, "zzz secret") == []
    assert await _search(other, "yyy hidden") == []
    assert await _search(other, "qqq private") == []


async def test_the_shortlist_reads_only_its_sport(db_session_factory: SessionFactory) -> None:
    logger = await _logger(db_session_factory, "shortlist-sport")
    await logger.exercises.create_custom(
        logger.user_id, sport_id="cycling", name="Zzz Spin", default_measure=ExerciseMeasure.TIME
    )

    assert await _search(logger, "zzz spin") == []


async def test_a_name_matches_the_closest_alias_whatever_the_tie(
    db_session_factory: SessionFactory,
) -> None:
    logger = await _logger(db_session_factory, "alias-closest")
    first = await logger.exercises.create_custom(
        logger.user_id, sport_id="gym", name="First", default_measure=ExerciseMeasure.REPS
    )
    second = await logger.exercises.create_custom(
        logger.user_id, sport_id="gym", name="Second", default_measure=ExerciseMeasure.REPS
    )
    await logger.exercises.add_user_alias(logger.user_id, first.id, "cable row wide")
    await logger.exercises.add_user_alias(logger.user_id, second.id, "cable row")

    matched = await logger.exercises.match_user_aliases(
        logger.user_id, sport_id="gym", names=["cable row", "unheard of"], near=0.75
    )

    assert matched == {"cable row": second.id}


async def _catalogue_id(factory: SessionFactory, source_ref: str) -> UUID:
    async with factory() as db:
        found: UUID = (
            await db.execute(
                text("SELECT id FROM exercises WHERE source_ref = :ref"), {"ref": source_ref}
            )
        ).scalar_one()
        return found
