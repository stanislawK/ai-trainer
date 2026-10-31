from ai_trainer.application.ports.exercises import (
    ExerciseCatalogueSourcePort,
    ExerciseRepositoryPort,
)
from ai_trainer.domain.exercises import CatalogueImportReport


async def import_catalogue(
    *, source: ExerciseCatalogueSourcePort, exercises: ExerciseRepositoryPort
) -> CatalogueImportReport:
    """Loads and validates everything before the database sees a row (ADR-0031), so a failed
    download or a broken file changes nothing."""
    catalogue = await source.load()
    write = await exercises.upsert_catalogue(catalogue.exercises)
    return CatalogueImportReport(
        total=len(catalogue.exercises), skipped=catalogue.skipped, write=write
    )
