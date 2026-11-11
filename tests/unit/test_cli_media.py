"""`generate-exercise-picture`'s one line and files per result (ADR-0031, #533): files only for a
made picture, exit 1 otherwise, and never the prompt, the brief's text or the admin's email."""

import argparse
import io
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from ai_trainer.cli_media import add_parser, generate_exercise_picture, slug, write_result
from ai_trainer.domain.media_generation import (
    GeneratedImage,
    ImageFailureCause,
    ImageGenerationFailed,
    ImageMediaType,
    PictureBrief,
    PictureFailed,
    PictureMade,
    PictureRefused,
    PictureResult,
    PictureSkipped,
    SpendCapReached,
)
from ai_trainer.settings import Settings

_EXERCISE_ID = UUID("00000000-0000-4000-8000-0000000c0533")
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
        (PictureRefused(reason="ambiguous_name"), "result=refused reason=ambiguous_name"),
        (
            PictureSkipped(reason="no_description", spent_usd=None),
            "result=skipped reason=no_description spent_usd=unknown",
        ),
        (
            PictureFailed(step="image", reason="timeout", spent_usd=None),
            "result=failed step=image reason=timeout spent_usd=unknown",
        ),
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
    ids=[
        "skipped",
        "refused",
        "ambiguous",
        "skipped_unknown",
        "failed_unknown",
        "over_cap",
        "cost_unknown",
        "failed",
    ],
)
def test_anything_but_a_picture_exits_1_and_writes_nothing(
    result: PictureResult, line: str, tmp_path: Path
) -> None:
    output = io.StringIO()

    code = write_result(result, out=tmp_path / "out", name="Pull-Up", output=output)

    assert code == 1
    assert output.getvalue() == f"generate-exercise-picture: {line}\n"
    assert not (tmp_path / "out").exists()


def _failed_line(failed: ImageGenerationFailed) -> str:
    output = io.StringIO()
    write_result(
        PictureFailed(step="image", reason="error", spent_usd=None, image=failed),
        out=Path("unused"),
        name="Pull-Up",
        output=output,
    )
    return output.getvalue()


def test_a_failed_image_line_prints_status_and_error() -> None:
    failed = ImageGenerationFailed(
        reason="error",
        cause="http_status",
        status=429,
        error_code="429",
        error_message='Rate "limited"',
    )

    assert _failed_line(failed) == (
        "generate-exercise-picture: result=failed step=image reason=error cause=http_status "
        'status=429 error=429 message="Rate \\"limited\\"" spent_usd=unknown\n'
    )


@pytest.mark.parametrize(
    "cause", ["empty_data", "bad_base64", "unsupported_type", "malformed_body"]
)
def test_each_image_cause_is_printed(cause: ImageFailureCause) -> None:
    failed = ImageGenerationFailed(reason="error", cause=cause, status=200)

    assert f" cause={cause} status=200 spent_usd=unknown" in _failed_line(failed)


def test_a_status_only_failure_prints_no_error_part() -> None:
    line = _failed_line(ImageGenerationFailed(reason="error", cause="http_status", status=502))

    assert line.endswith("reason=error cause=http_status status=502 spent_usd=unknown\n")
    assert " error=" not in line
    assert "message=" not in line


def test_cap_reached_cost_unknown_prints_the_template() -> None:
    output = io.StringIO()
    result = SpendCapReached(
        spent_usd=None,
        price_usd=Decimal("0.0336"),
        cap_usd=Decimal("0.25"),
        unknown_cost_template="media.picture_brief",
    )

    write_result(result, out=Path("unused"), name="Pull-Up", output=output)

    assert output.getvalue() == (
        "generate-exercise-picture: result=cap_reached reason=cost_unknown "
        "template=media.picture_brief spent_usd=unknown price_usd=0.0336 cap_usd=0.25\n"
    )


def _made(
    media_type: ImageMediaType = "image/png", spent: Decimal | None = Decimal("0.0340")
) -> PictureMade:
    return PictureMade(
        exercise_id=_EXERCISE_ID,
        brief=_BRIEF,
        image=GeneratedImage(data=b"img", media_type=media_type),
        spent_usd=spent,
    )


@pytest.mark.parametrize(
    ("media_type", "extension"),
    [("image/jpeg", "jpg"), ("image/png", "png"), ("image/webp", "webp")],
    ids=["jpeg", "png", "webp"],
)
def test_the_picture_is_written_with_its_types_extension(
    media_type: ImageMediaType, extension: str, tmp_path: Path
) -> None:
    output = io.StringIO()

    code = write_result(_made(media_type), out=tmp_path, name="Pull-Up", output=output)

    assert code == 0
    assert (tmp_path / f"pull-up.{extension}").read_bytes() == b"img"
    assert (tmp_path / "pull-up.brief.json").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["pull-up.brief.json", f"pull-up.{extension}"]
    )
    assert output.getvalue().endswith("spent_usd=0.034\n")
    assert _BRIEF.image_prompt not in output.getvalue()


def test_a_picture_of_unknown_spend_prints_unknown(tmp_path: Path) -> None:
    output = io.StringIO()

    write_result(_made(spent=None), out=tmp_path, name="Pull-Up", output=output)

    assert output.getvalue().endswith("spent_usd=unknown\n")


def test_an_unwritable_directory_is_a_failed_line_not_a_traceback(tmp_path: Path) -> None:
    blocker = tmp_path / "out"
    blocker.write_text("a file where the directory should be")
    output = io.StringIO()

    code = write_result(_made(), out=blocker, name="Pull-Up", output=output)

    assert code == 1
    assert output.getvalue() == (
        "generate-exercise-picture: result=failed reason=write_error spent_usd=0.034\n"
    )


def test_a_failed_write_leaves_no_partial_pair(tmp_path: Path) -> None:
    # The brief's own path is a directory, so its rename fails after the picture's.
    (tmp_path / "pull-up.brief.json").mkdir()
    output = io.StringIO()

    code = write_result(_made(), out=tmp_path, name="Pull-Up", output=output)

    assert code == 1
    assert output.getvalue() == (
        "generate-exercise-picture: result=failed reason=write_error spent_usd=0.034\n"
    )
    assert [path.name for path in tmp_path.iterdir()] == ["pull-up.brief.json"]
    assert (tmp_path / "pull-up.brief.json").is_dir()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Barbell Bench Press (Wide)", "barbell-bench-press-wide"),
        ("Wyciskanie żołnierskie", "wyciskanie-zolnierskie"),
        ("Übung für Straße", "ubung-fur-strasse"),
        ("  ", f"exercise-{_EXERCISE_ID}"),
        ("引体向上", f"exercise-{_EXERCISE_ID}"),
    ],
    ids=["words", "polish", "german", "blank", "no_latin"],
)
def test_slug(name: str, expected: str) -> None:
    assert slug(name, exercise_id=_EXERCISE_ID) == expected


def test_store_and_out_are_both_optional_flags() -> None:
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))

    both = parser.parse_args(["generate-exercise-picture", "Pull-Up", "--store", "--out", "d"])
    neither = parser.parse_args(["generate-exercise-picture", "Pull-Up"])

    assert (both.store, both.out) == (True, Path("d"))
    assert (neither.store, neither.out) == (False, None)


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
