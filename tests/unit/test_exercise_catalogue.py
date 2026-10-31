"""The catalogue's import rules (ADR-0031, ticket #375), on rows built here."""

import json
from pathlib import Path

import pytest
import yaml

from ai_trainer.domain.exercises import ExerciseCatalogueError, ExerciseMeasure, ExerciseText
from ai_trainer.domain.sports.gym.catalogue import (
    CatalogueOverrides,
    DatasetPin,
    DatasetRow,
    ExtraExercise,
    build_catalogue,
    clean_name,
    derive_measure,
)
from tests.exercise_support import COMMIT, FETCHED_ON, SAMPLE

_PIN = DatasetPin(commit=COMMIT, fetched_on=FETCHED_ON)
_WORDS = {"ez": "EZ", "pov": "POV", "v.": "v."}


def _row(ref: str, name: str, *, equipment: str = "barbell") -> DatasetRow:
    return DatasetRow(
        id=ref,
        name=name,
        equipment=equipment,
        body_part="chest",
        target="pectorals",
        secondary_muscles=("triceps",),
        instructions={"en": "Press.", "pl": "Wypchnij.", "es": "Empuja."},
        instruction_steps={"en": ("Press.",), "pl": ("Wypchnij.",), "es": ("Empuja.",)},
    )


def _extra(ref: str = "extra", name: str = "Face Pull") -> ExtraExercise:
    return ExtraExercise(
        ref=ref,
        name=name,
        equipment="cable",
        body_part="shoulders",
        target="delts",
        secondary_muscles=("traps",),
        default_measure=ExerciseMeasure.LOAD_REPS,
        texts=(ExerciseText(lang="en", instructions="Pull.", steps=("Pull.",)),),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("barbell bench press", "Barbell Bench Press"),
        ("arms overhead full sit-up (male)", "Arms Overhead Full Sit-Up"),
        ("twisted leg raise (female)", "Twisted Leg Raise"),
        ("barbell biceps curl (with arm blaster)", "Barbell Biceps Curl (With Arm Blaster)"),
        ("barbell full squat (back pov)", "Barbell Full Squat (Back POV)"),
        ("ez barbell spider curl", "EZ Barbell Spider Curl"),
        ("barbell upright row v. 2", "Barbell Upright Row v. 2"),
        ("3/4 sit-up", "3/4 Sit-Up"),
        ("45° side bend", "45° Side Bend"),
    ],
)
def test_clean_name_title_cases_and_drops_the_gender_suffix(raw: str, expected: str) -> None:
    assert clean_name(raw, _WORDS) == expected


@pytest.mark.parametrize(
    ("equipment", "measure"),
    [
        ("body weight", ExerciseMeasure.REPS),
        ("band", ExerciseMeasure.BAND_REPS),
        ("resistance band", ExerciseMeasure.BAND_REPS),
        ("barbell", ExerciseMeasure.LOAD_REPS),
        ("stability ball", ExerciseMeasure.LOAD_REPS),
    ],
)
def test_derive_measure_follows_the_equipment(equipment: str, measure: ExerciseMeasure) -> None:
    assert derive_measure(equipment) == measure


def test_build_applies_skip_rename_measure_and_the_supplement() -> None:
    built = build_catalogue(
        [_row("1", "bench press"), _row("2", "skip me"), _row("3", "sled 45в° press")],
        overrides=CatalogueOverrides(
            skip=frozenset({"2"}),
            rename={"3": "Sled 45° Press"},
            measure={"1": ExerciseMeasure.TIME},
        ),
        extras=[_extra()],
        pin=_PIN,
    )

    assert built.skipped == 1
    by_ref = {(row.source, row.source_ref): row for row in built.exercises}
    assert set(by_ref) == {
        ("exercises-dataset", "1"),
        ("exercises-dataset", "3"),
        ("ai-trainer", "extra"),
    }
    assert by_ref["exercises-dataset", "1"].default_measure == ExerciseMeasure.TIME
    assert by_ref["exercises-dataset", "3"].name == "Sled 45° Press"
    assert by_ref["exercises-dataset", "3"].default_measure == ExerciseMeasure.LOAD_REPS
    assert by_ref["ai-trainer", "extra"].name == "Face Pull"


def test_build_keeps_english_and_polish_texts_only() -> None:
    built = build_catalogue(
        [_row("1", "bench press")], overrides=CatalogueOverrides(), extras=[], pin=_PIN
    )

    assert [text.lang for text in built.exercises[0].texts] == ["en", "pl"]
    assert built.exercises[0].sport_id == "gym"


def test_attribution_names_the_mit_data_not_the_media_credit() -> None:
    records = json.loads(SAMPLE.read_text())
    assert all("Gym visual" in record["attribution"] for record in records)

    built = build_catalogue(
        [DatasetRow.model_validate(record) for record in records[:1]],
        overrides=CatalogueOverrides(),
        extras=[_extra()],
        pin=_PIN,
    )

    dataset_row, extra_row = built.exercises
    assert dataset_row.attribution.startswith("Exercise data: Hasan Emir ")
    assert dataset_row.attribution.endswith(
        ", exercises-dataset (MIT), commit 7455efa, fetched 2026-10-31"
    )
    assert "Gym visual" not in dataset_row.attribution
    assert extra_row.attribution == "ai-trainer"


@pytest.mark.parametrize(
    ("rows", "extras", "overrides"),
    [
        ([_row("1", "bench press"), _row("2", "Bench Press (male)")], [], CatalogueOverrides()),
        ([_row("1", "face pull")], [_extra()], CatalogueOverrides()),
        (
            [_row("1", "bench press"), _row("2", "row")],
            [],
            CatalogueOverrides(rename={"2": "bench  PRESS"}),
        ),
        (
            [_row("1", "bench press"), _row("2", "bench-press")],
            [],
            CatalogueOverrides(),
        ),
    ],
    ids=["after-cleaning", "dataset-and-supplement", "after-a-rename", "punctuation-only"],
)
def test_build_refuses_two_rows_with_one_name(
    rows: list[DatasetRow], extras: list[ExtraExercise], overrides: CatalogueOverrides
) -> None:
    with pytest.raises(ExerciseCatalogueError, match=r"(?i)bench press|face pull"):
        build_catalogue(rows, overrides=overrides, extras=extras, pin=_PIN)


@pytest.mark.parametrize(
    "overrides",
    [
        CatalogueOverrides(skip=frozenset({"gone"})),
        CatalogueOverrides(rename={"gone": "Gone"}),
        CatalogueOverrides(measure={"gone": ExerciseMeasure.TIME}),
    ],
    ids=["skip", "rename", "measure"],
)
def test_build_refuses_an_override_naming_an_unknown_row(overrides: CatalogueOverrides) -> None:
    with pytest.raises(ExerciseCatalogueError, match="gone"):
        build_catalogue([_row("1", "bench press")], overrides=overrides, extras=[], pin=_PIN)


def test_build_refuses_two_rows_with_one_key() -> None:
    with pytest.raises(ExerciseCatalogueError, match="share a key"):
        build_catalogue(
            [_row("1", "bench press"), _row("1", "row")],
            overrides=CatalogueOverrides(),
            extras=[],
            pin=_PIN,
        )


@pytest.mark.parametrize("name", ["(male)", "x" * 201], ids=["empty-after-cleaning", "too-long"])
def test_build_refuses_a_name_the_table_would_refuse(name: str) -> None:
    with pytest.raises(ExerciseCatalogueError, match="wrong length"):
        build_catalogue([_row("1", name)], overrides=CatalogueOverrides(), extras=[], pin=_PIN)


@pytest.mark.parametrize(
    "langs",
    [("pl",), ("en", "en"), ("en", "de")],
    ids=["no-english", "repeated", "not-imported"],
)
def test_an_extra_exercise_needs_english_and_only_imported_languages(
    langs: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="languages"):
        ExtraExercise(
            ref="extra",
            name="Face Pull",
            equipment="cable",
            body_part="shoulders",
            target="delts",
            secondary_muscles=(),
            default_measure=ExerciseMeasure.LOAD_REPS,
            texts=tuple(
                ExerciseText(lang=lang, instructions="Pull.", steps=("Pull.",)) for lang in langs
            ),
        )


def test_a_row_without_polish_text_is_invalid() -> None:
    with pytest.raises(ValueError, match="pl text"):
        DatasetRow(
            id="1",
            name="press",
            equipment="barbell",
            body_part="chest",
            target="pectorals",
            secondary_muscles=(),
            instructions={"en": "Press."},
            instruction_steps={"en": ("Press.",)},
        )


def test_the_committed_files_are_valid_and_hold_face_pull() -> None:
    directory = Path("data/catalogue")

    overrides = CatalogueOverrides.model_validate(
        yaml.safe_load((directory / "overrides.yaml").read_text())
    )
    extras = yaml.safe_load((directory / "extra.yaml").read_text())["exercises"]

    assert overrides.skip == {
        "0422",
        "0576",
        "0656",
        "1371",
        "1394",
        "1628",
        "1731",
        "1766",
        "2800",
        "2801",
    }
    assert overrides.rename["1759"] == "Single Leg Squat (Pistol)"
    assert [ExtraExercise.model_validate(entry).name for entry in extras] == ["Face Pull"]


def test_the_committed_files_keep_the_mit_notice() -> None:
    notice = Path("data/catalogue/LICENSE.exercises-dataset").read_text()

    assert "Copyright (c) 2026 Hasan Emir " in notice
    assert "MIT License" in notice
