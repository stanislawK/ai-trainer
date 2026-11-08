"""`ai-trainer generate-exercise-picture` (ADR-0031 *Picture generation*, #533).

    uv run ai-trainer generate-exercise-picture "<exercise name>" --out <dir> [--admin <email>]

An admin generates one picture of one catalogue exercise, to look at before anything is linked:
the brief is written from the exercise's public texts and its `search_knowledge` passages, then
the image model draws it. It writes `<dir>/<slug>.brief.json` and `<dir>/<slug>.jpg` (or
`.png`), and nothing to the media tables. Every call is an `llm_calls` row under the admin; the
run stops before a call that would take it past `IMAGE_GENERATION_SPEND_CAP_USD`, and when a
call's cost is unknown. It costs money, so it is run on purpose, never from a test.

`--admin` defaults to the one `ADMIN_EMAILS` entry; the email must be listed there and belong to
an active account. It prints one line and never the prompt, the brief or the email:
`generate-exercise-picture: result=written brief=<path> picture=<path> spent_usd=<x>` with exit
code 0, or `result=skipped|refused|cap_reached|failed reason=<...> spent_usd=<x|unknown>` with
exit code 1, and then no file is written."""

import argparse
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TextIO, assert_never

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.application.ports.media_generation import PictureBriefWriterPort
from ai_trainer.domain.media_generation import (
    PictureFailed,
    PictureMade,
    PictureRefused,
    PictureResult,
    PictureSkipped,
    SpendCapReached,
)
from ai_trainer.main import build_exercise_picture_service
from ai_trainer.settings import Settings

GENERATE_EXERCISE_PICTURE = "generate-exercise-picture"
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class PictureHooks:
    """For tests: the transport every OpenRouter HTTP call goes through, and a brief writer in
    place of the text model."""

    transport: httpx2.AsyncBaseTransport | None = None
    brief_writer: PictureBriefWriterPort | None = None


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    picture = commands.add_parser(
        GENERATE_EXERCISE_PICTURE,
        help="Generate one catalogue exercise's brief and picture into a directory (costs money).",
    )
    picture.add_argument("name", help="The catalogue exercise's name, e.g. 'Pull-Up'.")
    picture.add_argument("--out", type=Path, required=True, help="Directory to write into.")
    picture.add_argument(
        "--admin", default=None, help="The admin's email (default: the one ADMIN_EMAILS entry)."
    )


def slug(name: str) -> str:
    return _NOT_SLUG.sub("-", name.lower()).strip("-") or "exercise"


def _usd(value: Decimal | None) -> str:
    return "unknown" if value is None else format(value.normalize(), "f")


async def generate_exercise_picture(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    args: argparse.Namespace,
    *,
    output: TextIO,
    hooks: PictureHooks | None = None,
) -> int:
    admin = args.admin or (settings.admin_emails[0] if len(settings.admin_emails) == 1 else None)
    if admin is None:
        output.write(f"{GENERATE_EXERCISE_PICTURE}: result=refused reason=admin_required\n")
        return 1
    hooks = hooks or PictureHooks()
    async with httpx2.AsyncClient(transport=hooks.transport) as http_client:
        service = build_exercise_picture_service(
            settings, session_factory, http_client=http_client, brief_writer=hooks.brief_writer
        )
        result = await service.generate(admin_email=admin, exercise_name=args.name)
    return write_result(result, out=args.out, name=args.name, output=output)


def write_result(result: PictureResult, *, out: Path, name: str, output: TextIO) -> int:
    """Writes the files only for a made picture, and prints the run's one line."""
    prefix = f"{GENERATE_EXERCISE_PICTURE}: result="
    match result:
        case PictureMade(brief=brief, image=image, spent_usd=spent):
            out.mkdir(parents=True, exist_ok=True)
            brief_path = out / f"{slug(name)}.brief.json"
            picture_path = out / f"{slug(name)}.{_EXTENSIONS[image.media_type]}"
            brief_path.write_text(brief.model_dump_json(indent=2) + "\n", encoding="utf-8")
            picture_path.write_bytes(image.data)
            output.write(
                f"{prefix}written brief={brief_path} picture={picture_path} "
                f"spent_usd={_usd(spent)}\n"
            )
            return 0
        case PictureSkipped(reason=reason, spent_usd=spent):
            output.write(f"{prefix}skipped reason={reason} spent_usd={_usd(spent)}\n")
        case PictureRefused(reason=reason):
            output.write(f"{prefix}refused reason={reason}\n")
        case SpendCapReached(spent_usd=spent, price_usd=price, cap_usd=cap):
            why = "cost_unknown" if spent is None else "over_cap"
            output.write(
                f"{prefix}cap_reached reason={why} spent_usd={_usd(spent)} "
                f"price_usd={_usd(price)} cap_usd={_usd(cap)}\n"
            )
        case PictureFailed(step=step, reason=reason, spent_usd=spent):
            output.write(f"{prefix}failed step={step} reason={reason} spent_usd={_usd(spent)}\n")
        case _:  # pragma: no cover -- exhaustive over PictureResult
            assert_never(result)
    return 1
