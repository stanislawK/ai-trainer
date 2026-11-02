"""Ordering the athlete's resistance bands (PRD-0010 B39, ADR-0015, ADR-0032, ticket #431): the
first band takes rank 1 without a question, a later new colour asks once where it goes among the
ranked bands, the answer renumbers `rank`, and a skipped colour is never asked again. Replayed on
PostgreSQL through the real reply path, as #414 does."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from ai_trainer.adapters.gym_band_order import SqlAlchemyBandOrder
from ai_trainer.application.reply import (
    BandPlaced,
    abandon_clarification,
    answer_clarification,
)
from ai_trainer.domain.clarification import (
    BAND_PURPOSE,
    ClarificationNotFoundError,
    ClarificationResolvedError,
    StoredClarification,
    UnknownOptionError,
)
from tests.integration.test_first_gym_session_replay import (
    SessionFactory,
    _extraction,
    _lift,
    _Replay,
    _replay,
)


async def _band_replay(factory: SessionFactory, sub: str, tmp_path: Path) -> _Replay:
    replay = await _replay(factory, sub, tmp_path)
    replay.services = replace(replay.services, band_orders={"gym": SqlAlchemyBandOrder(factory)})
    return replay


async def _log_band(replay: _Replay, band: str) -> None:
    await replay.log(
        f"{band} band rows 3x10",
        _extraction("2026-10-02", _lift("band rows", sets=3, reps=10, band=band)),
        picks={"band rows": "Resistance Band Row"},
    )


async def _asked(replay: _Replay) -> list[StoredClarification]:
    return [
        question
        for question in await replay.clarifications.list_shown(
            replay.user_id, replay.athlete.conversation_id
        )
        if question.purpose == BAND_PURPOSE
    ]


async def _open(replay: _Replay) -> StoredClarification:
    [question] = [q for q in await _asked(replay) if q.status == "open"]
    return question


async def _answer(replay: _Replay, question: StoredClarification, option_id: str) -> BandPlaced:
    answered = await answer_clarification(
        replay.user_id,
        question.id,
        [option_id],
        timezone="Europe/Warsaw",
        services=replay.services,
    )
    assert isinstance(answered, BandPlaced)
    return answered


async def test_the_first_band_is_rank_1_without_a_question(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-first", tmp_path)

    await _log_band(replay, "red")

    assert await replay.bands() == {"red": 1}
    assert await _asked(replay) == []


async def test_a_second_band_offers_lighter_and_heavier_than_the_ranked_one(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-second", tmp_path)
    await _log_band(replay, "red")

    await _log_band(replay, "black")

    question = await _open(replay)
    assert question.clarification.subject == "black"
    assert [(o.copy_key, o.params["order"]) for o in question.clarification.options] == [
        ("band_place_first", "black, red"),
        ("band_place_last", "red, black"),
    ]
    assert question.draft_id is None


async def test_asking_writes_nothing_but_the_question(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-writes", tmp_path)
    await _log_band(replay, "red")

    await _log_band(replay, "black")

    assert await replay.bands() == {"red": 1, "black": None}


async def test_picking_between_two_bands_ranks_it_and_moves_the_heavier_one(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-between", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "green")
    await _answer(replay, await _open(replay), "place:2")
    await _log_band(replay, "black")

    placed = await _answer(replay, await _open(replay), "place:2")

    assert await replay.bands() == {"red": 1, "black": 2, "green": 3}
    assert placed.clarification.status == "answered"
    assert placed.clarification.answer_option_ids == ("place:2",)
    assert placed.next is None


async def test_a_ranked_or_already_asked_band_is_not_asked_again(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-once", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")
    await _answer(replay, await _open(replay), "place:2")

    await _log_band(replay, "black")
    await _log_band(replay, "RED")

    assert len(await _asked(replay)) == 1
    assert await replay.bands() == {"red": 1, "black": 2}


async def test_skipping_keeps_the_rank_null_and_the_band_is_not_asked_again(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-skip", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")
    question = await _open(replay)

    skipped = await abandon_clarification(replay.user_id, question.id, services=replay.services)
    await _log_band(replay, "black")

    assert isinstance(skipped, BandPlaced)
    assert skipped.clarification.status == "abandoned"
    assert skipped.next is None
    assert await replay.bands() == {"red": 1, "black": None}
    assert [q.status for q in await _asked(replay)] == ["abandoned"]


async def test_a_second_unplaced_band_of_a_log_is_asked_after_the_first_is_answered(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-chain", tmp_path)
    await _log_band(replay, "red")
    await replay.log(
        "black and green band rows",
        _extraction(
            "2026-10-02",
            _lift("band rows", sets=3, reps=10, band="black"),
            _lift("band rows", sets=3, reps=10, band="green"),
        ),
        picks={"band rows": "Resistance Band Row"},
    )
    first = await _open(replay)

    placed = await _answer(replay, first, "place:2")

    assert placed.next is not None
    assert placed.next.clarification.subject == "green"
    assert [o.id for o in placed.next.clarification.options] == ["place:1", "place:2", "place:3"]
    # The fixed test clock sits before the replay's confirm time, so compare without order.
    assert sorted(q.status for q in await _asked(replay)) == ["answered", "open"]


async def test_options_never_name_another_athletes_band(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    mine = await _band_replay(db_session_factory, "bands-mine", tmp_path)
    theirs = await _band_replay(db_session_factory, "bands-theirs", tmp_path)
    await _log_band(theirs, "secret")
    await _log_band(mine, "red")

    await _log_band(mine, "black")

    question = await _open(mine)
    assert "secret" not in question.clarification.model_dump_json()
    assert await theirs.bands() == {"secret": 1}
    with pytest.raises(ClarificationNotFoundError):
        await answer_clarification(
            theirs.user_id, question.id, ["place:1"], timezone="UTC", services=theirs.services
        )


async def test_a_place_the_question_never_offered_is_refused_and_writes_nothing(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-unknown", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")
    question = await _open(replay)

    with pytest.raises(UnknownOptionError):
        await _answer(replay, question, "place:7")

    assert await replay.bands() == {"red": 1, "black": None}
    assert (await _open(replay)).id == question.id


async def test_an_answered_question_cannot_be_answered_again(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-twice", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")
    question = await _open(replay)
    await _answer(replay, question, "place:2")

    with pytest.raises(ClarificationResolvedError):
        await _answer(replay, question, "place:1")

    assert await replay.bands() == {"red": 1, "black": 2}


async def _seed_ranked(replay: _Replay, labels: list[str], *, start: int = 1) -> None:
    async with replay.factory() as db:
        for rank, label in enumerate(labels, start=start):
            await db.execute(
                text(
                    "INSERT INTO athlete_bands (id, user_id, label, rank)"
                    " VALUES (gen_random_uuid(), :u, :l, :r)"
                ),
                {"u": replay.user_id, "l": label, "r": rank},
            )
        await db.commit()


async def test_more_ranked_bands_than_the_cap_narrow_in_place_then_place(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-narrow", tmp_path)
    await _seed_ranked(replay, ["b1", "b2", "b3", "b4", "b5", "b6"])
    await _log_band(replay, "black")
    question = await _open(replay)
    assert [o.id for o in question.clarification.options] == ["narrow:1-4", "narrow:5-7"]

    narrowed = await _answer(replay, question, "narrow:5-7")

    assert narrowed.clarification.status == "open"
    assert narrowed.clarification.id == question.id
    assert [o.id for o in narrowed.clarification.clarification.options] == [
        "place:5",
        "place:6",
        "place:7",
    ]
    assert await replay.bands() == {
        "b1": 1,
        "b2": 2,
        "b3": 3,
        "b4": 4,
        "b5": 5,
        "b6": 6,
        "black": None,
    }

    await _answer(replay, narrowed.clarification, "place:7")

    assert (await replay.bands())["black"] == 7


async def test_a_new_band_waits_while_another_band_question_is_open(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-wait", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")

    await _log_band(replay, "green")

    [only] = await _asked(replay)
    assert only.clarification.subject == "black"
    placed = await _answer(replay, only, "place:2")
    assert placed.next is not None
    assert placed.next.clarification.subject == "green"


async def test_placing_a_band_that_is_already_ranked_changes_nothing(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-ranked", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")
    await _answer(replay, await _open(replay), "place:2")

    await SqlAlchemyBandOrder(db_session_factory).place(replay.user_id, "red", 2)

    assert await replay.bands() == {"red": 1, "black": 2}


async def test_a_sport_with_no_band_scale_has_no_band_question_to_answer(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-no-scale", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")
    question = await _open(replay)
    replay.services = replace(replay.services, band_orders={})

    with pytest.raises(ClarificationNotFoundError):
        await _answer(replay, question, "place:2")


async def test_a_card_whose_ranking_changed_is_asked_again_and_ranks_nothing(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-stale", tmp_path)
    await _seed_ranked(replay, ["b1", "b2", "b3", "b4", "b5", "b6"])
    await _log_band(replay, "black")
    question = await _open(replay)
    await _seed_ranked(replay, ["b7"], start=7)

    again = await _answer(replay, question, "narrow:5-7")

    assert again.clarification.status == "open"
    assert again.clarification.id == question.id
    assert [o.id for o in again.clarification.clarification.options] == ["narrow:1-4", "narrow:5-8"]
    assert (await replay.bands())["black"] is None


async def test_skipping_asks_the_next_unplaced_band_of_the_log(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-skip-next", tmp_path)
    await _log_band(replay, "red")
    await replay.log(
        "black and green band rows",
        _extraction(
            "2026-10-02",
            _lift("band rows", sets=3, reps=10, band="black"),
            _lift("band rows", sets=3, reps=10, band="green"),
        ),
        picks={"band rows": "Resistance Band Row"},
    )
    first = await _open(replay)

    skipped = await abandon_clarification(replay.user_id, first.id, services=replay.services)

    assert isinstance(skipped, BandPlaced)
    assert skipped.clarification.status == "abandoned"
    assert skipped.next is not None
    assert skipped.next.clarification.subject == "green"
    assert await replay.bands() == {"red": 1, "black": None, "green": None}


class _Failing(SqlAlchemyBandOrder):
    async def place(self, user_id: UUID, label: str, rank: int) -> None:
        raise RuntimeError("the database went away")


async def test_a_band_that_could_not_be_ranked_leaves_the_card_to_tap_again(
    db_session_factory: SessionFactory, tmp_path: Path
) -> None:
    replay = await _band_replay(db_session_factory, "bands-fail", tmp_path)
    await _log_band(replay, "red")
    await _log_band(replay, "black")
    question = await _open(replay)
    replay.services = replace(replay.services, band_orders={"gym": _Failing(db_session_factory)})

    with pytest.raises(RuntimeError):
        await _answer(replay, question, "place:2")

    assert (await _open(replay)).id == question.id
    assert await replay.bands() == {"red": 1, "black": None}
