"""`generate-exercise-picture`'s one line and files per result (ADR-0031, #533): files only for a
made picture, exit 1 otherwise, and never the prompt, the brief's text or the admin's email."""

import argparse
import io
from decimal import Decimal
from pathlib import Path

import pytest

from ai_trainer.cli_media import generate_exercise_picture, slug, write_result
from ai_trainer.domain.media_generation import (
    GeneratedImage,
    PictureBrief,
    PictureFailed,
    PictureMade,
    PictureRefused,
    PictureResult,
    PictureSkipped,
    SpendCapReached,
)
from ai_trainer.settings import Settings

_BRIEF = PictureBrief(
    exercise_name="Pull-Up",
    start_position="Hanging, arms extended",
    end_position="Chest at the bar",
    image_prompt="One person hanging, then chest at the bar. Plain background. No text.",
)


@pytest.mark.parametrize(
    ("result", "line"),
    [
        (
            PictureSkipped(reason="no_such_exercise", spent_usd=Decimal(0)),
            "result=skipped reason=no_such_exercise spent_usd=0",
        ),
        (PictureRefused(reason="not_admin"), "result=refused reason=not_admin"),
        (
            SpendCapReached(
                spent_usd=Decimal("0.2"), price_usd=Decimal("0.0336"), cap_usd=Decimal("0.25")
            ),
            "result=cap_reached reason=over_cap spent_usd=0.2 price_usd=0.0336 cap_usd=0.25",
        ),
        (
            SpendCapReached(spent_usd=None, price_usd=Decimal("0.0336"), cap_usd=Decimal("0.25")),
            "result=cap_reached reason=cost_unknown spent_usd=unknown price_usd=0.0336 "
            "cap_usd=0.25",
        ),
        (
            PictureFailed(step="brief", reason="timeout", spent_usd=Decimal("0.0001")),
            "result=failed step=brief reason=timeout spent_usd=0.0001",
        ),
    ],
    ids=["skipped", "refused", "over_cap", "cost_unknown", "failed"],
)
def test_anything_but_a_picture_exits_1_and_writes_nothing(
    result: PictureResult, line: str, tmp_path: Path
) -> None:
    output = io.StringIO()

    code = write_result(result, out=tmp_path / "out", name="Pull-Up", output=output)

    assert code == 1
    assert output.getvalue() == f"generate-exercise-picture: {line}\n"
    assert not (tmp_path / "out").exists()


def test_a_png_is_written_as_png(tmp_path: Path) -> None:
    output = io.StringIO()
    made = PictureMade(
        brief=_BRIEF,
        image=GeneratedImage(data=b"png", media_type="image/png"),
        spent_usd=Decimal("0.0340"),
    )

    code = write_result(made, out=tmp_path, name="Pull-Up", output=output)

    assert code == 0
    assert (tmp_path / "pull-up.png").read_bytes() == b"png"
    assert output.getvalue().endswith("spent_usd=0.034\n")
    assert _BRIEF.image_prompt not in output.getvalue()


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Barbell Bench Press (Wide)", "barbell-bench-press-wide"), ("  ", "exercise")],
    ids=["words", "blank"],
)
def test_slug(name: str, expected: str) -> None:
    assert slug(name) == expected


@pytest.mark.parametrize(
    "admins", [[], ["one@example.com", "two@example.com"]], ids=["none", "two"]
)
async def test_without_admin_and_not_one_admin_email_it_refuses(
    admins: list[str], tmp_path: Path
) -> None:
    settings = Settings.model_construct(admin_emails=admins)
    output = io.StringIO()
    args = argparse.Namespace(name="Pull-Up", out=tmp_path, admin=None)

    code = await generate_exercise_picture(
        settings, lambda: pytest.fail("no database"), args, output=output
    )

    assert code == 1
    assert output.getvalue() == "generate-exercise-picture: result=refused reason=admin_required\n"
