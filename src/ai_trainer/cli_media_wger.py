"""`ai-trainer import-wger-pictures` (ADR-0031 *First media source*, #514, #552).

    uv run ai-trainer import-wger-pictures [--limit N]

Reads wger's licence list, English exercise names and image listing (at most `--limit` images),
and stores each image whose author is named, whose licence wger lists and whose English name
matches a gym catalogue exercise at `EXERCISE_NEAR_MATCH_THRESHOLD` by trigram similarity: the
file goes byte for byte to the media bucket under `exercises/<exercise id>/<sha256>.<ext>`, then
its `licensed`, `stored` asset and a `suggested` link are written in one transaction. Below the
threshold nothing is downloaded. An image whose `sha256` is already catalogued, `removed`
included, is skipped, so a second run writes nothing. Nothing is shown until an admin approves
it (PRD 0012 F24).

With no media bucket configured it is refused before any request:
`import-wger-pictures: result=refused reason=storage_not_configured`, exit 1. Otherwise it prints
one line per image, `stored image=<id> exercise=<id> asset=<id>`, `skipped reason=<no_author|
unknown_licence|known_sha256> image=<id>` or `unmatched image=<id>`, then
`import-wger-pictures: result=done stored=<n> skipped=<n> unmatched=<n>` with exit 0, or
`result=failed reason=<wger_error|wger_timeout|storage_error|database_error> stored=<n> ...` with
exit 1 when the run stopped: what it stored before stays whole, and no object is left without its
row. It never prints a key, a URL or an author.
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

import httpx2
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.media_sources import ImportReport
from ai_trainer.main import build_media_storage, build_wger_picture_import
from ai_trainer.settings import Settings

IMPORT_WGER_PICTURES = "import-wger-pictures"
_PREFIX = f"{IMPORT_WGER_PICTURES}: "


@dataclass(frozen=True, slots=True)
class WgerHooks:
    """For tests: the transport every wger request goes through, a media bucket in place of the
    configured one, and the sport whose catalogue rows are matched (gym otherwise)."""

    transport: httpx2.AsyncBaseTransport | None = None
    storage: ObjectStoragePort | None = None
    sport_id: str | None = None


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("at least 1")
    return number


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = commands.add_parser(
        IMPORT_WGER_PICTURES,
        help="Store wger's pictures that match a catalogue exercise as suggested media.",
    )
    command.add_argument(
        "--limit", type=_positive, default=None, help="Read at most this many wger images."
    )


async def import_wger_pictures(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    args: argparse.Namespace,
    *,
    output: TextIO,
    hooks: WgerHooks | None = None,
) -> int:
    hooks = hooks or WgerHooks()
    storage = hooks.storage or build_media_storage(settings)
    if storage is None:
        output.write(f"{_PREFIX}result=refused reason=storage_not_configured\n")
        return 1
    async with httpx2.AsyncClient(transport=hooks.transport) as http_client:
        extra = {} if hooks.sport_id is None else {"sport_id": hooks.sport_id}
        importer = build_wger_picture_import(
            settings, session_factory, storage=storage, http_client=http_client, **extra
        )
        report = await importer.run(limit=args.limit)
    return write_report(report, output=output)


def write_report(report: ImportReport, *, output: TextIO) -> int:
    for stored in report.stored:
        output.write(
            f"{_PREFIX}stored image={stored.image_id} exercise={stored.exercise_id} "
            f"asset={stored.asset_id}\n"
        )
    for skipped in report.skipped:
        output.write(f"{_PREFIX}skipped reason={skipped.reason} image={skipped.image_id}\n")
    for image_id in report.unmatched:
        output.write(f"{_PREFIX}unmatched image={image_id}\n")
    counts = (
        f"stored={len(report.stored)} skipped={len(report.skipped)} "
        f"unmatched={len(report.unmatched)}"
    )
    if report.failed is not None:
        output.write(f"{_PREFIX}result=failed reason={report.failed} {counts}\n")
        return 1
    output.write(f"{_PREFIX}result=done {counts}\n")
    return 0
