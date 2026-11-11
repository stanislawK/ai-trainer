"""`ai-trainer generate-exercise-picture` (ADR-0031 *Picture generation*, #533).

    uv run ai-trainer generate-exercise-picture "<exercise name>" [--out <dir>] [--store] \\
        [--admin <email>]

An admin generates one picture of one catalogue exercise, to look at before anything is linked:
the brief is written from the exercise's public texts and its `search_knowledge` passages, then
the image model draws it. With `--out` it writes `<dir>/<slug>.brief.json` and
`<dir>/<slug>.jpg` (or `.png`, `.webp`), both or neither. With `--store` (#535) it also uploads
the picture to the media bucket and writes its asset and a `suggested` link in one transaction
(`StoreExercisePicture`): a storage or database failure leaves no row, and the picture shows
only once an admin approves it. Neither flag is refused (`reason=no_destination`), and `--store`
with no media bucket configured is refused (`reason=storage_not_configured`), both before any
call. Without `--store` it writes nothing to the media tables. Every call is an `llm_calls` row
under the admin; the run stops before a call that would take it past
`IMAGE_GENERATION_SPEND_CAP_USD`, and when a call's cost is unknown. A name in more than one
sport is refused (`reason=ambiguous_name`) before any call. It costs money, so it is run on
purpose, never from a test.

`--admin` defaults to the one `ADMIN_EMAILS` entry; the email must be listed there and belong to
an active account. It prints one line and never the prompt, the brief or the email:
`generate-exercise-picture: result=written brief=<path> picture=<path> spent_usd=<x>` with exit
code 0 (`result=stored asset=<id> exercise=<id> link=suggested spent_usd=<x>` for `--store`, with
the two paths appended when `--out` is given too), or
`result=skipped|refused|cap_reached|failed reason=<...> spent_usd=<x|unknown>` with exit code 1
(#543: a failed image call adds `cause=http_status|transport|malformed_body|empty_data|
bad_base64|unsupported_type|unexpected`, then `status=<n>`, `error=<provider code, or the
exception class for transport>` and `message=<quoted>` when known, neither holding the prompt or
a key, and a `cost_unknown` stop adds `template=<id>` of the
call with no cost),
and then no file is written (a failed write is `result=failed reason=write_error`, a failed
upload `reason=storage_error`, a failed row write `reason=database_error`, and the same bytes
already catalogued `reason=already_stored`).
"""

import argparse
import contextlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TextIO, assert_never
from uuid import UUID

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.application.exercise_picture_store import StoreExercisePicture
from ai_trainer.application.ports.media_generation import PictureBriefWriterPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.media_generation import (
    ImageGenerationFailed,
    ImageMediaType,
    PictureFailed,
    PictureMade,
    PictureRefused,
    PictureResult,
    PictureSkipped,
    PictureStoreFailed,
    SpendCapReached,
)
from ai_trainer.main import (
    build_exercise_picture_service,
    build_exercise_picture_store,
    build_media_storage,
)
from ai_trainer.settings import Settings

GENERATE_EXERCISE_PICTURE = "generate-exercise-picture"
_EXTENSIONS: dict[ImageMediaType, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
# Latin letters NFKD doesn't split into a base letter and a mark.
_UNSPLIT = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe"})


@dataclass(frozen=True, slots=True)
class PictureHooks:
    """For tests: the transport every OpenRouter HTTP call goes through, a brief writer in
    place of the text model, and a media bucket in place of the configured one."""

    transport: httpx2.AsyncBaseTransport | None = None
    brief_writer: PictureBriefWriterPort | None = None
    storage: ObjectStoragePort | None = None


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    picture = commands.add_parser(
        GENERATE_EXERCISE_PICTURE,
        help="Generate one catalogue exercise's brief and picture into a directory (costs money).",
    )
    picture.add_argument("name", help="The catalogue exercise's name, e.g. 'Pull-Up'.")
    picture.add_argument("--out", type=Path, default=None, help="Directory to write into.")
    picture.add_argument(
        "--store",
        action="store_true",
        help="Upload the picture to the media bucket and save it as suggested media.",
    )
    picture.add_argument(
        "--admin", default=None, help="The admin's email (default: the one ADMIN_EMAILS entry)."
    )


def slug(name: str, *, exercise_id: UUID) -> str:
    """The file name's stem: the name's letters with diacritics folded, else the exercise's ID,
    so two names never share a stem by falling back."""
    folded = unicodedata.normalize("NFKD", name.lower().translate(_UNSPLIT))
    ascii_name = folded.encode("ascii", "ignore").decode()
    return _NOT_SLUG.sub("-", ascii_name).strip("-") or f"exercise-{exercise_id}"


def _write_pair(made: PictureMade, *, out: Path, stem: str) -> tuple[Path, Path]:
    """Writes the picture and the brief to temporary names in `out`, then renames both; a
    failure removes what it wrote, so there is never one without the other."""
    out.mkdir(parents=True, exist_ok=True)
    brief_path = out / f"{stem}.brief.json"
    picture_path = out / f"{stem}.{_EXTENSIONS[made.image.media_type]}"
    brief = (made.brief.model_dump_json(indent=2) + "\n").encode()
    temporary: list[Path] = []
    renamed: list[Path] = []
    try:
        for data in (made.image.data, brief):
            fd, name = tempfile.mkstemp(dir=out, prefix=f".{stem}.", suffix=".tmp")
            temporary.append(Path(name))
            with os.fdopen(fd, "wb") as file:
                file.write(data)
        for source, target in zip(temporary, (picture_path, brief_path), strict=True):
            source.replace(target)
            renamed.append(target)
    except OSError:
        for path in (*temporary, *renamed):
            with contextlib.suppress(OSError):
                path.unlink()
        raise
    return brief_path, picture_path


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
    if args.out is None and not args.store:
        output.write(f"{GENERATE_EXERCISE_PICTURE}: result=refused reason=no_destination\n")
        return 1
    store: StoreExercisePicture | None = None
    if args.store:
        storage = hooks.storage or build_media_storage(settings)
        if storage is None:
            output.write(
                f"{GENERATE_EXERCISE_PICTURE}: result=refused reason=storage_not_configured\n"
            )
            return 1
        store = build_exercise_picture_store(settings, session_factory, storage=storage)
    async with httpx2.AsyncClient(transport=hooks.transport) as http_client:
        service = build_exercise_picture_service(
            settings, session_factory, http_client=http_client, brief_writer=hooks.brief_writer
        )
        result = await service.generate(admin_email=admin, exercise_name=args.name)
    if store is not None and isinstance(result, PictureMade):
        return await _store_made(result, store, out=args.out, name=args.name, output=output)
    return write_result(result, out=args.out, name=args.name, output=output)


async def _store_made(
    made: PictureMade,
    store: StoreExercisePicture,
    *,
    out: Path | None,
    name: str,
    output: TextIO,
) -> int:
    """Writes the files when `--out` is given, then stores the picture as suggested media."""
    prefix = f"{GENERATE_EXERCISE_PICTURE}: result="
    written: tuple[Path, Path] | None = None
    if out is not None:
        try:
            brief_path, picture_path = _write_pair(
                made, out=out, stem=slug(name, exercise_id=made.exercise_id)
            )
        except OSError:
            output.write(f"{prefix}failed reason=write_error spent_usd={_usd(made.spent_usd)}\n")
            return 1
        written = (brief_path, picture_path)
    stored = await store.store(made)
    if isinstance(stored, PictureStoreFailed):
        # A failed run leaves no file behind, as the module says.
        _remove_files(written)
        output.write(f"{prefix}failed reason={stored.reason} spent_usd={_usd(stored.spent_usd)}\n")
        return 1
    output.write(
        f"{prefix}stored asset={stored.asset_id} exercise={stored.exercise_id} "
        f"link=suggested spent_usd={_usd(stored.spent_usd)}{_files_part(written)}\n"
    )
    return 0


def _files_part(written: tuple[Path, Path] | None) -> str:
    return "" if written is None else f" brief={written[0]} picture={written[1]}"


def _remove_files(written: tuple[Path, Path] | None) -> None:
    for path in written or ():
        with contextlib.suppress(OSError):
            path.unlink()


def _failure_part(failed: ImageGenerationFailed | None) -> str:
    """` cause=... status=... error=... message=...`, each only when the provider gave it."""
    if failed is None or failed.cause is None:
        return ""
    part = f" cause={failed.cause}"
    if failed.status is not None:
        part += f" status={failed.status}"
    if failed.error_code is not None:
        part += f" error={failed.error_code}"
    if failed.error_message is not None:
        part += f" message={json.dumps(failed.error_message)}"
    return part


def write_result(result: PictureResult, *, out: Path, name: str, output: TextIO) -> int:
    """Writes the files only for a made picture, and prints the run's one line."""
    prefix = f"{GENERATE_EXERCISE_PICTURE}: result="
    match result:
        case PictureMade(exercise_id=exercise_id, spent_usd=spent):
            try:
                brief_path, picture_path = _write_pair(
                    result, out=out, stem=slug(name, exercise_id=exercise_id)
                )
            except OSError:
                output.write(f"{prefix}failed reason=write_error spent_usd={_usd(spent)}\n")
                return 1
            output.write(
                f"{prefix}written brief={brief_path} picture={picture_path} "
                f"spent_usd={_usd(spent)}\n"
            )
            return 0
        case PictureSkipped(reason=reason, spent_usd=spent):
            output.write(f"{prefix}skipped reason={reason} spent_usd={_usd(spent)}\n")
        case PictureRefused(reason=reason):
            output.write(f"{prefix}refused reason={reason}\n")
        case SpendCapReached(
            spent_usd=spent, price_usd=price, cap_usd=cap, unknown_cost_template=template
        ):
            why = "cost_unknown" if spent is None else "over_cap"
            named = f" template={template}" if template else ""
            output.write(
                f"{prefix}cap_reached reason={why}{named} spent_usd={_usd(spent)} "
                f"price_usd={_usd(price)} cap_usd={_usd(cap)}\n"
            )
        case PictureFailed(step=step, reason=reason, spent_usd=spent, image=image):
            output.write(
                f"{prefix}failed step={step} reason={reason}{_failure_part(image)} "
                f"spent_usd={_usd(spent)}\n"
            )
        case _:  # pragma: no cover -- exhaustive over PictureResult
            assert_never(result)
    return 1
