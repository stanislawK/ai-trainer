"""The gym plugin: payload v2 and its v1 upcast, load normalization and the extraction it is
built from (ADR-0006, ADR-0022, ADR-0032, tickets #159 and #395)."""

from datetime import date
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from ai_trainer.domain.clarification import ChoiceOption
from ai_trainer.domain.sports.base import SessionEnvelope
from ai_trainer.domain.sports.gym.edit import apply_gym_edit
from ai_trainer.domain.sports.gym.extraction import RPE_OF_EFFORT, Effort, GymExtraction
from ai_trainer.domain.sports.gym.normalize import normalize_load, to_kg
from ai_trainer.domain.sports.gym.payload import (
    GYM_PAYLOAD_VERSION,
    Exercise,
    GymPayload,
    GymSet,
    Load,
    measure_of,
)
from ai_trainer.domain.sports.registry import default_sport_registry


@pytest.mark.parametrize(
    ("value", "unit", "kg"),
    [
        (100, "kg", 100.0),
        (225, "lb", 102.06),
        (45, "lb", 20.41),
        (62.5, "kg", 62.5),
        (1, "lb", 0.45),
    ],
)
def test_a_load_is_converted_to_kg_in_code(value: float, unit: Any, kg: float) -> None:
    assert to_kg(value, unit) == kg


def test_a_load_in_lb_is_stored_in_kg_with_its_original_text() -> None:
    assert normalize_load(" 225 lbs ", 225, "lb") == Load(
        original="225 lbs", value=225, unit="lb", kg=102.06
    )


def test_a_tiny_load_that_rounds_to_0_kg_is_still_kept() -> None:
    assert normalize_load("0.001 lb", 0.001, "lb").kg == 0


def _extraction(**overrides: Any) -> GymExtraction:
    values: dict[str, Any] = {
        "occurred_on": "2026-10-02",
        "date_source": "assumed_today",
        "activities": [
            {
                "name": "squat",
                "sets": 5,
                "reps": 5,
                "load": {"original": "100kg", "value": 100, "unit": "kg"},
            },
            {"name": "pull-ups", "sets": 3},
        ],
    }
    return GymExtraction.model_validate(values | overrides)


def test_5x5_squat_at_100kg_then_3_sets_of_pull_ups_becomes_two_exercises() -> None:
    payload = _extraction().to_payload()

    assert payload.sport == "gym"
    squat = GymSet(reps=5, load=Load(original="100kg", value=100, unit="kg", kg=100))
    assert payload.activities == [
        Exercise(name_as_typed="squat", measure="load_reps", sets=[squat] * 5),
        Exercise(name_as_typed="pull-ups", measure="reps", sets=[GymSet()] * 3),
    ]


def test_a_load_in_lb_becomes_kg_and_keeps_what_was_typed() -> None:
    payload = _extraction(
        activities=[
            {
                "name": "deadlift",
                "sets": 1,
                "reps": 5,
                "load": {"original": "315 lb", "value": 315, "unit": "lb"},
                "rir": 2,
            }
        ]
    ).to_payload()

    (exercise,) = payload.activities
    assert exercise.sets[0].load == Load(original="315 lb", value=315, unit="lb", kg=142.88)
    assert exercise.rir == 2


def test_a_load_with_no_unit_is_read_as_kg_and_keeps_its_number_as_typed() -> None:
    # Owner-chosen at #159's plan gate: "Bench 3x8 @ 60" is 60 kg, editable before Confirm.
    payload = _extraction(
        activities=[
            {"name": "bench", "sets": 3, "reps": 8, "load": {"original": "60", "value": 60}}
        ]
    ).to_payload()

    (exercise,) = payload.activities
    assert exercise.sets[0].load == Load(original="60", value=60, unit="kg", kg=60)


def test_an_exercise_name_is_kept_as_typed_without_surrounding_space() -> None:
    payload = _extraction(activities=[{"name": "  Romanian deadlift "}]).to_payload()

    assert payload.activities == [
        Exercise(name_as_typed="Romanian deadlift", measure="reps", sets=[GymSet()])
    ]


def test_extraction_dates_are_absolute() -> None:
    extraction = _extraction(occurred_on="2026-10-01", date_source="relative")

    assert (extraction.occurred_on, extraction.date_source) == (date(2026, 10, 1), "relative")


@pytest.mark.parametrize(
    "exercise",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "squat", "sets": 0},
        {"name": "squat", "reps": 0},
        {"name": "squat", "rir": -1},
        {"name": "squat", "rir": 11},
        {"name": "squat", "load": {"original": "0 kg", "value": 0, "unit": "kg"}},
        {"name": "squat", "load": {"original": "100 st", "value": 100, "unit": "st"}},
        {"name": "squat", "load": {"original": "", "value": 100, "unit": "kg"}},
        {"name": "squat", "implements": 0},
        {"name": "squat", "implements": 5},
        {"name": "squat", "band": "  "},
        {"name": "plank", "duration": {"value": 0, "unit": "s"}},
        {"name": "plank", "duration": {"value": 45, "unit": "h"}},
        {"name": "plank", "duration": {"value": 86401, "unit": "s"}},
        {"name": "plank", "duration": {"value": 1441, "unit": "min"}},
        {"name": "row", "distance": {"value": 10000, "unit": "km"}},
        {"name": "squat", "sets": 101},
        {"name": "row", "distance": {"value": 0, "unit": "m"}},
        {"name": "row", "distance": {"value": 1, "unit": "mi"}},
    ],
)
def test_extraction_rejects_invalid_exercises(exercise: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[exercise])


def test_extraction_needs_at_least_one_exercise() -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[])


def test_payload_is_version_eight_and_tagged_gym() -> None:
    """A v3 document, which has no `jev_pick`, reads as it is with the flag off (#399), and a v4
    one, which has no `candidate_scores`, with no scores (#579)."""
    assert GYM_PAYLOAD_VERSION == 8
    payload = GymPayload.model_validate(
        {"activities": [{"name_as_typed": "pull-ups", "measure": "reps", "sets": [{"reps": 8}]}]}
    )

    assert payload.model_dump(mode="json") == {
        "sport": "gym",
        "activities": [
            {
                "exercise_id": None,
                "keep_name": False,
                "jev_pick": False,
                "candidate_scores": {},
                "name_as_typed": "pull-ups",
                "measure": "reps",
                "per_side": False,
                "implements": None,
                "rir": None,
                "sets": [
                    {
                        "reps": 8,
                        "load": None,
                        "duration_s": None,
                        "distance_m": None,
                        "band_label": None,
                        "incline_pct": None,
                        "machine_level": None,
                        "watts": None,
                    }
                ],
                "circuit": None,
                "rounds": None,
            }
        ],
    }


_V5_TREADMILL: dict[str, Any] = {
    "sport": "gym",
    "activities": [
        {
            "exercise_id": None,
            "keep_name": False,
            "jev_pick": False,
            "candidate_scores": {},
            "name_as_typed": "treadmill",
            "measure": "time",
            "per_side": False,
            "implements": None,
            "rir": None,
            "sets": [
                {
                    "reps": None,
                    "load": None,
                    "duration_s": 1200,
                    "distance_m": None,
                    "band_label": None,
                }
            ],
        }
    ],
}
# A v6 draft (#626) is the same shape as a v5 one: neither knows a machine setting.
_V6_ROWER: dict[str, Any] = {
    **_V5_TREADMILL,
    "activities": [
        {
            **_V5_TREADMILL["activities"][0],
            "name_as_typed": "rower",
            "measure": "distance_time",
            "sets": [{"duration_s": 470, "distance_m": 2000.0, "band_label": None}],
        }
    ],
}


@pytest.mark.parametrize("stored", [_V5_TREADMILL, _V6_ROWER], ids=["v5", "v6"])
def test_a_v5_and_v6_draft_read_with_no_machine_settings(stored: dict[str, Any]) -> None:
    """A draft written before machine settings (B44, #630) reads as it was, with incline, level
    and watts empty, and reads the same again once written back (ADR-0022 invariant 4)."""
    payload = GymPayload.model_validate(stored)

    (gym_set,) = payload.activities[0].sets
    assert (gym_set.incline_pct, gym_set.machine_level, gym_set.watts) == (None, None, None)
    assert payload.activities[0].measure == stored["activities"][0]["measure"]
    assert GymPayload.model_validate(payload.model_dump(mode="json")) == payload


@pytest.mark.parametrize(
    "fields",
    [
        {"incline_pct": 40.1},
        {"incline_pct": -10.1},
        {"machine_level": 0},
        {"machine_level": 51},
        {"watts": 0},
        {"watts": 2001},
    ],
)
def test_a_machine_setting_out_of_range_is_refused(fields: dict[str, Any]) -> None:
    """The bounds of ADR-0032's CHECKs (#618 amendment): incline -10 to 40 %, level 1 to 50,
    watts 1 to 2000."""
    with pytest.raises(ValidationError):
        GymSet.model_validate(fields)


def test_machine_settings_alone_hold_no_measure() -> None:
    """A set with only incline, level or watts counts as blank (ADR-0032 #618 amendment), so
    the first set with a measure decides, and a later timed distance still upcasts."""
    payload = GymPayload.model_validate(
        {
            "activities": [
                {
                    "name_as_typed": "rower",
                    "measure": "time",
                    "sets": [{"watts": 150}, {"distance_m": 2000, "duration_s": 470}],
                }
            ]
        }
    )

    assert payload.activities[0].measure == "distance_time"
    assert GymSet(incline_pct=-10, machine_level=50, watts=2000).incline_pct == -10


def test_a_v1_exercise_reads_as_v2() -> None:
    load = {"original": "45kg", "value": 45, "unit": "kg", "kg": 45}
    payload = GymPayload.model_validate(
        {"activities": [{"name": "Bench", "sets": 2, "reps": 8, "load": load, "rir": 1}]}
    )

    gym_set = GymSet(reps=8, load=Load.model_validate(load))
    assert payload.activities == [
        Exercise(name_as_typed="Bench", measure="load_reps", rir=1, sets=[gym_set, gym_set])
    ]


@pytest.mark.parametrize(
    ("fields", "measure"),
    [
        ({"sets": 3, "duration": {"value": 45, "unit": "s"}}, "time"),
        ({"distance": {"value": 40, "unit": "m"}}, "distance"),
        (
            {"distance": {"value": 2000, "unit": "m"}, "duration": {"value": 470, "unit": "s"}},
            "distance_time",
        ),
        ({"sets": 3, "reps": 10, "band": "red"}, "band_reps"),
        ({"reps": 5, "load": {"original": "6 kg", "value": 6, "unit": "kg"}}, "load_reps"),
        ({"sets": 4}, "reps"),
        # A weighted plank is held for time, and its load is still kept.
        (
            {
                "duration": {"value": 1, "unit": "min"},
                "load": {"original": "10 kg", "value": 10, "unit": "kg"},
            },
            "time",
        ),
    ],
)
def test_measure_comes_from_what_the_log_gave(fields: dict[str, Any], measure: str) -> None:
    payload = _extraction(activities=[{"name": "x", **fields}]).to_payload()

    assert payload.activities[0].measure == measure


def test_measure_of_distance_only_and_time_only_unchanged() -> None:
    """A carry with only a distance stays `distance` and a plank with only a time stays `time`;
    only a set holding both is `distance_time` (ADR-0032, #626)."""
    assert measure_of(GymSet(distance_m=40)) == "distance"
    assert measure_of(GymSet(duration_s=45)) == "time"
    assert measure_of(GymSet(distance_m=2000, duration_s=470)) == "distance_time"
    assert measure_of(GymSet(distance_m=0, duration_s=470)) == "distance_time"


@pytest.mark.parametrize("measure", ["time", "distance"])
def test_upcast_old_draft_time_with_distance_reads_distance_time(measure: str) -> None:
    """A v5 draft read its rower as `time`; v6 reads it as `distance_time` from the first set
    that holds a value, and leaves a plank or a carry as it was (ADR-0022 invariant 4, #626)."""
    payload = GymPayload.model_validate(
        {
            "activities": [
                {
                    "name_as_typed": "rower",
                    "measure": measure,
                    "sets": [{}, {"distance_m": 2000, "duration_s": 470}],
                },
                {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 45}]},
                {"name_as_typed": "carry", "measure": "distance", "sets": [{"distance_m": 40}]},
                {
                    "name_as_typed": "sled",
                    "measure": "load_reps",
                    "sets": [{"distance_m": 20, "duration_s": 30}],
                },
            ]
        }
    )

    assert [exercise.measure for exercise in payload.activities] == [
        "distance_time",
        "time",
        "distance",
        "load_reps",
    ]


def test_a_duration_in_minutes_and_a_distance_in_km_are_converted_in_code() -> None:
    payload = _extraction(
        activities=[
            {"name": "plank", "duration": {"value": 1.5, "unit": "min"}},
            {"name": "row", "distance": {"value": 1.5, "unit": "km"}},
        ]
    ).to_payload()

    assert payload.activities[0].sets[0].duration_s == 90
    assert payload.activities[1].sets[0].distance_m == 1500


def test_each_side_implements_and_band_reach_the_payload() -> None:
    payload = _extraction(
        activities=[{"name": "rows", "sets": 2, "per_side": True, "implements": 2, "band": " red "}]
    ).to_payload()

    (exercise,) = payload.activities
    assert (exercise.per_side, exercise.implements) == (True, 2)
    assert [gym_set.band_label for gym_set in exercise.sets] == ["red", "red"]


@pytest.mark.parametrize(
    "exercise",
    [
        {"name": ""},
        {"name": "squat", "sets": 0},
        {"name": "squat", "load": {"original": "100 kg", "value": 100, "unit": "kg", "kg": -1}},
        {"name": "squat", "load": {"original": "100 kg", "value": 100, "unit": None, "kg": 100}},
        {"name": "squat", "rir": 11},
        {"name_as_typed": "squat", "measure": "reps", "sets": []},
        {"name_as_typed": "squat", "measure": "reps", "sets": [{}] * 101},
        {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 86401}]},
        {"name_as_typed": "row", "measure": "distance", "sets": [{"distance_m": 1e7}]},
        {"name": "squat", "sets": 101},
        {"name_as_typed": "squat", "measure": "weight", "sets": [{}]},
        {"name_as_typed": "squat", "measure": "reps", "implements": 5, "sets": [{}]},
    ],
)
def test_payload_rejects_invalid_exercises(exercise: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GymPayload.model_validate({"activities": [exercise]})


def test_payload_needs_at_least_one_exercise() -> None:
    with pytest.raises(ValidationError):
        GymPayload.model_validate({"activities": []})


def test_the_registry_wires_gym_to_its_extraction_template_and_payload() -> None:
    plugin = default_sport_registry().get("gym")

    assert (plugin.icon, plugin.label_key) == ("dumbbell", "sport.gym.label")
    assert plugin.extraction is not None
    assert (plugin.extraction.template_id, plugin.extraction.version) == ("gym.extract", 8)
    assert plugin.extraction.output_type is GymExtraction
    assert plugin.extraction.payload_type is GymPayload
    assert plugin.extraction.payload_version == GYM_PAYLOAD_VERSION
    assert plugin.extraction.apply_edit is apply_gym_edit


def _scheme(name: str, reps: int, load_kg: float, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "sets": 1,
        "reps": reps,
        "load": {"original": f"{load_kg:g}", "value": load_kg, "unit": "kg"},
    } | fields


def _pyramid(*activities: dict[str, Any]) -> GymPayload:
    return _extraction(activities=list(activities)).to_payload()


def _reps_at_kg(payload: GymPayload) -> list[list[tuple[int | None, float | None]]]:
    return [
        [
            (gym_set.reps, None if gym_set.load is None else gym_set.load.kg)
            for gym_set in entry.sets
        ]
        for entry in payload.activities
    ]


def test_a_pyramid_of_three_schemes_becomes_one_entry_of_three_sets() -> None:
    payload = _pyramid(_scheme("bench", 10, 60), _scheme("bench", 8, 70), _scheme("bench", 6, 80))

    (bench,) = payload.activities
    assert bench.name_as_typed == "bench"
    assert _reps_at_kg(payload) == [[(10, 60), (8, 70), (6, 80)]]


def test_2x5_then_1x3_merge_into_three_sets_in_typed_order() -> None:
    payload = _pyramid(_scheme("squat", 5, 100, sets=2), _scheme("squat", 3, 110))

    assert _reps_at_kg(payload) == [[(5, 100), (5, 100), (3, 110)]]


def test_a_name_differing_only_in_case_and_space_merges_under_the_first() -> None:
    payload = _pyramid(_scheme("Bench", 10, 60), _scheme(" bench ", 8, 70))

    (bench,) = payload.activities
    assert bench.name_as_typed == "Bench"
    assert len(bench.sets) == 2


@pytest.mark.parametrize(
    "second",
    [
        {"rir": 2},
        {"per_side": True},
        {"implements": 2},
        {"duration": {"value": 30, "unit": "s"}, "load": None, "reps": None},
        {"name": "squat"},
    ],
    ids=["rir", "side", "implements", "measure", "name"],
)
def test_entries_that_differ_stay_separate(second: dict[str, Any]) -> None:
    payload = _pyramid(_scheme("bench", 10, 60), _scheme("bench", 8, 70) | second)

    assert len(payload.activities) == 2


def test_the_same_lift_with_another_exercise_between_stays_separate() -> None:
    payload = _pyramid(
        _scheme("bench", 10, 60), {"name": "pull-ups", "sets": 3}, _scheme("bench", 8, 70)
    )

    assert [entry.name_as_typed for entry in payload.activities] == ["bench", "pull-ups", "bench"]


def test_a_merge_past_100_sets_leaves_the_entries_separate() -> None:
    payload = _pyramid(_scheme("bench", 5, 60, sets=60), _scheme("bench", 5, 70, sets=41))

    assert [len(entry.sets) for entry in payload.activities] == [60, 41]


def test_a_merge_up_to_100_sets_still_merges() -> None:
    payload = _pyramid(_scheme("bench", 5, 60, sets=60), _scheme("bench", 5, 70, sets=40))

    assert [len(entry.sets) for entry in payload.activities] == [100]


def test_a_movement_answer_still_names_every_scheme() -> None:
    extraction = _extraction(
        activities=[_scheme("bench", 10, 60), _scheme("bench", 8, 70)]
    ).answered(
        ChoiceOption(
            id="bench press",
            copy_key="gym_reading_movement",
            params={"exercise": "bench", "movement": "bench press", "others": ""},
        )
    )

    (entry,) = extraction.to_payload().activities
    assert entry.name_as_typed == "bench press"


# Stretching, mobility, classes and indoor cardio are gym (PRD 0013 B41, ticket #622): each is
# an entry of timed sets, and a class's length and effort are also the session's.


def _timed(name: str, value: float, unit: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "duration": {"value": value, "unit": unit}} | fields


def test_twenty_minutes_of_stretching_is_one_set_of_1200_s() -> None:
    (entry,) = _extraction(activities=[_timed("stretching", 20, "min")]).to_payload().activities

    assert (entry.name_as_typed, entry.measure, entry.per_side) == ("stretching", "time", False)
    assert [gym_set.duration_s for gym_set in entry.sets] == [1200]


def test_a_stretch_each_side_is_one_per_side_set_of_30_s() -> None:
    extraction = _extraction(activities=[_timed("hip flexor stretch", 30, "s", per_side=True)])

    (entry,) = extraction.to_payload().activities
    assert (entry.name_as_typed, entry.per_side) == ("hip flexor stretch", True)
    assert [gym_set.duration_s for gym_set in entry.sets] == [30]


def test_a_class_is_one_timed_entry_with_the_session_duration_and_rpe() -> None:
    extraction = _extraction(
        activities=[_timed("yoga class", 60, "min")],
        session_duration={"value": 60, "unit": "min"},
        effort="hard",
    )

    (entry,) = extraction.to_payload().activities
    assert entry.name_as_typed == "yoga class"
    assert [gym_set.duration_s for gym_set in entry.sets] == [3600]
    assert extraction.envelope() == SessionEnvelope(duration_min=60, rpe=7)


def test_thirty_minutes_on_the_treadmill_is_kept_as_a_timed_entry() -> None:
    (entry,) = _extraction(activities=[_timed("treadmill", 30, "min")]).to_payload().activities

    assert (entry.name_as_typed, entry.measure) == ("treadmill", "time")
    assert [gym_set.duration_s for gym_set in entry.sets] == [1800]


def test_no_effort_word_leaves_rpe_empty() -> None:
    extraction = _extraction(activities=[_timed("stretching", 20, "min")])

    assert extraction.envelope() == SessionEnvelope(duration_min=None, rpe=None)


def test_a_stated_rpe_is_kept_as_typed() -> None:
    """A number the athlete typed wins over an effort word beside it."""
    assert _extraction(rpe=8).envelope().rpe == 8
    assert _extraction(rpe=8, effort="easy").envelope().rpe == 8


@pytest.mark.parametrize("rpe", [0, 11])
def test_a_stated_rpe_outside_1_to_10_is_refused(rpe: int) -> None:
    with pytest.raises(ValidationError):
        _extraction(rpe=rpe)


def test_effort_table_covers_every_word() -> None:
    assert set(RPE_OF_EFFORT) == set(get_args(Effort.__value__))
    assert RPE_OF_EFFORT == {
        "very_easy": 2,
        "easy": 3,
        "moderate": 5,
        "hard": 7,
        "very_hard": 9,
        "max": 10,
    }
    for word, rpe in RPE_OF_EFFORT.items():
        assert _extraction(effort=word).envelope().rpe == rpe


@pytest.mark.parametrize(
    ("value", "unit", "minutes"),
    [
        (1, "h", 60),
        (1.5, "h", 90),
        (90, "min", 90),
        (45, "s", 1),
        (61, "s", 2),
        (0.4, "s", 1),
        (24, "h", 1440),
    ],
)
def test_session_duration_converts_to_minutes(value: float, unit: str, minutes: int) -> None:
    extraction = _extraction(session_duration={"value": value, "unit": unit})

    assert extraction.envelope().duration_min == minutes


def test_a_session_longer_than_a_day_is_refused() -> None:
    with pytest.raises(ValidationError):
        _extraction(session_duration={"value": 25, "unit": "h"})


def test_a_set_timed_in_hours_is_kept_in_seconds() -> None:
    (entry,) = _extraction(activities=[_timed("spin class", 1, "h")]).to_payload().activities

    assert [gym_set.duration_s for gym_set in entry.sets] == [3600]


@pytest.mark.parametrize(("typed", "kept"), [(5.55, 5.6), (2.04, 2.0), (-9.96, -10.0)])
def test_an_incline_is_kept_to_the_one_decimal_gym_sets_holds(typed: float, kept: float) -> None:
    # `gym_sets.incline_pct` is numeric(4,1): the draft shows what the row will hold (#630).
    assert GymSet(incline_pct=typed).incline_pct == kept
