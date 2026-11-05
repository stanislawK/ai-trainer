from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.exercises import (
    ExerciseCatalogueSourcePort,
    ExerciseRepositoryPort,
)
from ai_trainer.application.ports.knowledge import CatalogueSourcesPort
from ai_trainer.domain.exercises import CatalogueExercise, CatalogueImportReport


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
    download or a broken file changes nothing. With `knowledge`, it then queues each catalogue
    source whose texts are stale or that failed, for the ingest worker, which embeds only what
    changed."""
    catalogue = await source.load()
    write = await exercises.upsert_catalogue(catalogue.exercises)
    queued = 0
    if knowledge is not None:
        queued = await knowledge.sources.queue(
            attribution=_dataset_attribution(catalogue.exercises),
            admin_emails=knowledge.admin_emails,
            now=knowledge.clock.now(),
        )
    return CatalogueImportReport(
        total=len(catalogue.exercises),
        skipped=catalogue.skipped,
        write=write,
        catalogue_queued=queued,
    )


def _dataset_attribution(exercises: Sequence[CatalogueExercise]) -> str | None:
    """The attribution most rows carry: the pinned dataset's (ADR-0031 G10). The few
    `extra.yaml` rows written in-house carry their own, and a source holds only one."""
    counts = Counter(exercise.attribution for exercise in exercises)
    return counts.most_common(1)[0][0] if counts else None
