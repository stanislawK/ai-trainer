"""The owner's first gym session, replayed from message to typed tables (PRD-0010 B37-B39,
ADR-0031, ADR-0032, ticket #414). Each message of `evals/field/gym/2026-10-28-first-gym-session.md`
goes through the real reply, the exercise card and the draft confirm on PostgreSQL, with the model's
extraction scripted as `gym.extract` would return it. What the tests assert is read back from
`gym_exercise_entries`, `gym_sets` and `athlete_bands` with plain SQL."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clarification_repository import SqlAlchemyClarificationRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.exercise_history import SqlAlchemyExerciseHistory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.exercises_dataset import PinnedDatasetCatalogueSource
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.reply import (
    ReplyDone,
    ReplyServices,
    SessionSettled,
    answer_clarification,
    start_reply,
)
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.clarification import KEEP_NAME_OPTION, StoredClarification
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.training import StoredDraft
from tests.activity_support import STORES
from tests.chat_support import (
    InMemoryChatRepository,
    InMemoryProfile,
    ScriptedConversation,
    reply_services,
)
from tests.exercise_support import (
    COMMIT,
    FETCHED_ON,
    SAMPLE,
    SAMPLE_CATALOGUE_DIR,
    dataset_transport,
)
from tests.integration.test_session_continuity_repository import (
    _START,
    _Athlete,
    _athlete,
    _Clock,
)
from tests.picker_support import ScriptedPicker

SessionFactory = Callable[[], AsyncSession]
_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)
_WEEK_LATER = _START + timedelta(days=7)

# Exercises the dataset sample lacks, as `extra.yaml` adds them to the real catalogue.
_EXTRAS = """\
exercises:
  - ref: zz-replay-lateral-raise
    name: Dumbbell Lateral Raise
    equipment: dumbbell
    body_part: shoulders
    target: delts
    secondary_muscles: [traps]
    default_measure: load_reps
    texts:
      - lang: en
        instructions: Raise both straight arms to the sides, then lower them.
        steps: [Raise both arms to the sides., Lower them slowly.]
  - ref: zz-replay-plank
    name: Plank
    equipment: body weight
    body_part: waist
    target: abs
    secondary_muscles: [shoulders]
    default_measure: time
    texts:
      - lang: en
        instructions: Hold a straight line on your forearms and toes.
        steps: [Hold a straight line.]
  - ref: zz-replay-anti-rotation
    name: Band Anti-Rotation Press
    equipment: band
    body_part: waist
    target: abs
    secondary_muscles: [shoulders]
    default_measure: band_reps
    texts:
      - lang: en
        instructions: Press the band away from your chest and resist the twist.
        steps: [Press the band out., Resist the twist.]
"""


def _lift(name: str, **measures: Any) -> dict[str, Any]:
    return {"name": name, **measures}


def _kg(value: float) -> dict[str, Any]:
    return {"original": f"{value:g}kg", "value": value, "unit": "kg"}


def _extraction(day: str, *lifts: dict[str, Any]) -> GymExtraction:
    return GymExtraction.model_validate(
        {"occurred_on": day, "date_source": "assumed_today", "activities": list(lifts)}
    )


class _Jev(ScriptedPicker):
    """Picks the shortlisted exercise with ID `picks_id` when there is one, otherwise none."""

    picks_id: UUID | None = None

    async def pick(
        self, user_id: UUID, name: str, candidates: Sequence[ExerciseCandidate]
    ) -> ExerciseCandidate | None:
        await super().pick(user_id, name, candidates)
        return next((c for c in candidates if c.id == self.picks_id), None)


@dataclass
class _Reply:
    draft: StoredDraft
    questions: tuple[StoredClarification, ...]


class _Replay:
    """One athlete sending the session's messages through the real reply path."""

    def __init__(self, factory: SessionFactory, athlete: _Athlete, catalogue: Path) -> None:
        self.factory = factory
        self.athlete = athlete
        self.user_id = athlete.user_id
        self.exercises = SqlAlchemyExerciseRepository(factory)
        self.history = SqlAlchemyExerciseHistory(factory)
        self.chat = SqlAlchemyChatRepository(factory)
        self.drafts = SqlAlchemySessionDraftRepository(factory, stores=STORES)
        self.clarifications = SqlAlchemyClarificationRepository(factory)
        self.conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]]
        )
        self.picker = _Jev()
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
            clarifications=self.clarifications,
            sessions=SqlAlchemyTrainingSessionRepository(factory, stores=STORES),
        )
        self._sent = 0

    async def send(
        self,
        message: str,
        extraction: GymExtraction,
        *,
        at: datetime | None = None,
        picks: Mapping[str, str | None] | None = None,
    ) -> _Reply:
        """Sends `message`; `picks` maps each typed name the app asks about to the candidate the
        athlete taps, or to `None` for "Keep my name"."""
        self._sent += 1
        self.conversation.extractions = [extraction]
        sent = await self.chat.add(
            self.user_id,
            self.athlete.conversation_id,
            ChatRole.USER,
            message,
            (at or _START) + timedelta(minutes=self._sent),
        )
        events = await start_reply(
            self.user_id, sent.id, timezone="Europe/Warsaw", services=self.services
        )
        *_, done = [event async for event in events]
        assert isinstance(done, ReplyDone), done
        [draft] = done.drafts
        for question in done.clarifications:
            draft = await self._answer(question, picks or {})
        return _Reply(draft, tuple(done.clarifications))

    async def _answer(
        self, question: StoredClarification, picks: Mapping[str, str | None]
    ) -> StoredDraft:
        typed = question.clarification.subject or ""
        assert typed.casefold() in picks, (
            f"the app asked about {typed!r}, which the test never answers"
        )
        wanted = picks[typed.casefold()]
        option = KEEP_NAME_OPTION if wanted is None else self._option(question, wanted)
        settled = await answer_clarification(
            self.user_id, question.id, [option], timezone="Europe/Warsaw", services=self.services
        )
        assert isinstance(settled, SessionSettled)
        return settled.draft

    @staticmethod
    def _option(question: StoredClarification, name: str) -> str:
        names = [option.params.get("name") for option in question.clarification.options]
        assert name in names, f"{name!r} is not offered, the card offers {names}"
        return next(o.id for o in question.clarification.options if o.params.get("name") == name)

    async def confirm(self, reply: _Reply) -> StoredDraft:
        return await self.drafts.confirm(
            self.user_id, reply.draft.id, confirmed_at=reply.draft.created_at + timedelta(seconds=1)
        )

    async def log(self, *args: Any, **kwargs: Any) -> _Reply:
        """Sends a message and confirms the draft it ends in."""
        reply = await self.send(*args, **kwargs)
        await self.confirm(reply)
        return reply

    async def entries(self, name_as_typed: str) -> list[dict[str, Any]]:
        async with self.factory() as db:
            rows = await db.execute(
                text(
                    "SELECT e.id, e.exercise_id, e.measure, e.per_side, e.implements"
                    " FROM gym_exercise_entries e JOIN training_sessions s ON s.id = e.session_id"
                    " WHERE e.user_id = :u AND e.name_as_typed = :n ORDER BY s.occurred_on"
                ),
                {"u": self.user_id, "n": name_as_typed},
            )
            return [dict(row._mapping) for row in rows]

    async def sets(self, entry_id: UUID) -> list[dict[str, Any]]:
        async with self.factory() as db:
            rows = await db.execute(
                text(
                    "SELECT reps, load_kg, duration_s, distance_m, band_label FROM gym_sets"
                    " WHERE user_id = :u AND entry_id = :e ORDER BY set_no"
                ),
                {"u": self.user_id, "e": entry_id},
            )
            return [
                {
                    k: float(v) if k in ("load_kg", "distance_m") and v is not None else v
                    for k, v in row._mapping.items()
                }
                for row in rows
            ]

    async def bands(self) -> dict[str, int | None]:
        async with self.factory() as db:
            rows = await db.execute(
                text("SELECT label, rank FROM athlete_bands WHERE user_id = :u"),
                {"u": self.user_id},
            )
            return {row[0]: row[1] for row in rows}


async def _replay(factory: SessionFactory, sub: str, tmp_path: Path) -> _Replay:
    (tmp_path / "overrides.yaml").write_bytes(
        (SAMPLE_CATALOGUE_DIR / "overrides.yaml").read_bytes()
    )
    (tmp_path / "extra.yaml").write_text(_EXTRAS, encoding="utf-8")
    source = PinnedDatasetCatalogueSource(
        commit=COMMIT,
        fetched_on=FETCHED_ON,
        directory=tmp_path,
        transport=dataset_transport(SAMPLE.read_bytes()),
    )
    await import_catalogue(source=source, exercises=SqlAlchemyExerciseRepository(factory))
    return _Replay(factory, await _athlete(factory, sub), tmp_path)


_BENCH = "Bench press 45kg 8 x3"
_RAISE = "4 series two hantles 6kg each - rising straight arms on both sides"
_ANTI = "Anti rotations 12 reps each side - red bungee (elastic) tape"


async def test_bench_press_stores_three_sets_of_eight_at_45_kg(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _replay(db_session_factory, "replay-bench", tmp_path)

    await replay.log(
        _BENCH,
        _extraction("2026-10-02", _lift("Bench press", sets=3, reps=8, load=_kg(45))),
        picks={"bench press": "Barbell Bench Press"},
    )

    [entry] = await replay.entries("Bench press")
    assert entry["measure"] == "load_reps"
    assert (
        await replay.sets(entry["id"])
        == [
            {"reps": 8, "load_kg": 45.0, "duration_s": None, "distance_m": None, "band_label": None}
        ]
        * 3
    )


async def test_lateral_raise_stores_four_sets_with_no_reps_and_two_6_kg_dumbbells(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _replay(db_session_factory, "replay-raise", tmp_path)

    await replay.log(
        _RAISE,
        _extraction(
            "2026-10-02",
            _lift("rising straight arms on both sides", sets=4, load=_kg(6), implements=2),
        ),
        picks={"rising straight arms on both sides": None},
    )

    [entry] = await replay.entries("rising straight arms on both sides")
    assert entry["implements"] == 2
    assert (
        await replay.sets(entry["id"])
        == [
            {
                "reps": None,
                "load_kg": 6.0,
                "duration_s": None,
                "distance_m": None,
                "band_label": None,
            }
        ]
        * 4
    )


async def test_anti_rotation_stores_12_reps_per_side_with_the_red_band(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _replay(db_session_factory, "replay-anti", tmp_path)

    await replay.log(
        _ANTI,
        _extraction("2026-10-02", _lift("Anti rotations", reps=12, per_side=True, band="red")),
        picks={"anti rotations": "Band Anti-Rotation Press"},
    )

    [entry] = await replay.entries("Anti rotations")
    assert entry["per_side"] is True
    assert entry["measure"] == "band_reps"
    assert await replay.sets(entry["id"]) == [
        {"reps": 12, "load_kg": None, "duration_s": None, "distance_m": None, "band_label": "red"}
    ]
    # The label is stored as named; whether the first band gets rank 1 is #400's, not asserted.
    assert "red" in await replay.bands()


async def test_a_week_later_rising_arms_maps_to_the_same_exercise_and_history_shows_6_then_7_kg(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _replay(db_session_factory, "replay-week", tmp_path)
    await replay.log(
        _RAISE,
        _extraction(
            "2026-10-02",
            _lift("rising straight arms on both sides", sets=4, load=_kg(6), implements=2),
        ),
        picks={"rising straight arms on both sides": None},
    )
    [first] = await replay.entries("rising straight arms on both sides")

    # Jev, scripted, is confident of the lift the athlete settled a week ago.
    replay.picker.picks_id = first["exercise_id"]

    again = await replay.log(
        "rising arms to sides 4x10 7kg",
        _extraction("2026-10-09", _lift("rising arms to sides", sets=4, reps=10, load=_kg(7))),
        at=_WEEK_LATER,
    )

    assert again.questions == ()
    assert [name for _, name, _ in replay.picker.asked] == ["rising arms to sides"]
    [second] = await replay.entries("rising arms to sides")
    assert second["exercise_id"] == first["exercise_id"]
    history = await replay.history.entries(replay.user_id, first["exercise_id"], limit=10)
    by_day = sorted(history, key=lambda entry: entry.occurred_on)
    assert [entry.sets[0].load_kg for entry in by_day] == [6.0, 7.0]


async def test_plank_stores_a_45_second_hold_with_no_reps_or_load(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _replay(db_session_factory, "replay-plank", tmp_path)

    await replay.log(
        "Plank 45s",
        _extraction("2026-10-02", _lift("Plank", duration={"value": 45, "unit": "s"})),
        picks={"plank": "Plank"},
    )

    [entry] = await replay.entries("Plank")
    assert entry["measure"] == "time"
    assert await replay.sets(entry["id"]) == [
        {"reps": None, "load_kg": None, "duration_s": 45, "distance_m": None, "band_label": None}
    ]


async def _log_two_bands(replay: _Replay) -> tuple[_Reply, _Reply]:
    red = await replay.log(
        "red band rows 3x10",
        _extraction("2026-10-02", _lift("band rows", sets=3, reps=10, band="red")),
        picks={"band rows": "Resistance Band Row"},
    )
    black = await replay.send(
        "black band rows 3x10",
        _extraction("2026-10-02", _lift("band rows", sets=3, reps=10, band="black")),
    )
    return red, black


async def test_no_band_is_ranked_without_the_athletes_answer(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _replay(db_session_factory, "replay-unranked", tmp_path)
    _, black = await _log_two_bands(replay)

    await replay.confirm(black)

    bands = await replay.bands()
    assert set(bands) == {"red", "black"}
    assert bands["black"] is None


async def test_a_second_band_colour_asks_once_which_is_harder(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _replay(db_session_factory, "replay-ask", tmp_path)
    _, black = await _log_two_bands(replay)
    await replay.confirm(black)

    asked = [
        question
        for question in await replay.clarifications.list_shown(
            replay.user_id, replay.athlete.conversation_id
        )
        if question.status == "open" and "red" in question.clarification.model_dump_json()
    ]

    assert len(asked) == 1
