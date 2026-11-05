from collections.abc import Sequence
from dataclasses import dataclass

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.exercises import (
    ExerciseCatalogueSourcePort,
    ExerciseRepositoryPort,
)
from ai_trainer.application.ports.knowledge import CatalogueSourcesPort
from ai_trainer.domain.exercises import CatalogueImportReport, CatalogueWrite


@dataclass(frozen=True, slots=True)
class CatalogueQueueing:
    """What queues the catalogue's knowledge sources after an import (ADR-0036's #427
    amendment): the sources, `ADMIN_EMAILS` for the calls' acting user, and the clock."""

    sources: CatalogueSourcesPort
    admin_emails: Sequence[str]
    clock: ClockPort


async def import_catalogue(
    *,
    source: ExerciseCatalogueSourcePort,
    exercises: ExerciseRepositoryPort,
    knowledge: CatalogueQueueing | None = None,
) -> CatalogueImportReport:
    """Loads and validates everything before the database sees a row (ADR-0031), so a failed
    download or a broken file changes nothing. With `knowledge`, an import that wrote anything
    queues the catalogue's sources for the ingest worker, which embeds only what changed."""
    catalogue = await source.load()
    write = await exercises.upsert_catalogue(catalogue.exercises)
    queued = 0
    if knowledge is not None:
        queued = await knowledge.sources.queue(
            changed=_wrote_anything(write),
            attribution=catalogue.exercises[0].attribution if catalogue.exercises else None,
            admin_emails=knowledge.admin_emails,
            now=knowledge.clock.now(),
        )
    return CatalogueImportReport(
        total=len(catalogue.exercises),
        skipped=catalogue.skipped,
        write=write,
        catalogue_queued=queued,
    )


def _wrote_anything(write: CatalogueWrite) -> bool:
    # An updated exercise may be a renamed one, whose chunks carry its name.
    return write.inserted + write.updated + write.texts_written > 0
