from datetime import date
from pathlib import Path
from typing import Annotated

import httpx2
import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ai_trainer.domain.exercises import (
    ExerciseCatalogueError,
    ExerciseDatasetError,
    ImportedCatalogue,
)
from ai_trainer.domain.sports.gym.catalogue import (
    CatalogueOverrides,
    DatasetPin,
    DatasetRow,
    ExtraExercise,
    build_catalogue,
)

# Only this one JSON file is ever requested, never the repository's `images/` or `videos/`.
_DATASET_URL = (
    "https://raw.githubusercontent.com/hasaneyldrm/exercises-dataset/{commit}/data/exercises.json"
)
# 17 MB, read by hand or from a release step, never on a request path.
_DOWNLOAD_TIMEOUT_SECONDS = 120.0
_ROWS: TypeAdapter[list[DatasetRow]] = TypeAdapter(Annotated[list[DatasetRow], Field(min_length=1)])


class _ExtraFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exercises: tuple[ExtraExercise, ...]


def _read_file[Model: BaseModel](path: Path, model: type[Model]) -> Model:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise ExerciseCatalogueError(f"{path.name} is invalid: {exc}") from exc


class PinnedDatasetCatalogueSource:
    """Implements `ExerciseCatalogueSourcePort` from one dataset commit and the committed files.
    Reads the files first, so a broken checkout never costs a download."""

    def __init__(
        self,
        *,
        commit: str,
        fetched_on: date,
        directory: Path,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._pin = DatasetPin(commit=commit, fetched_on=fetched_on)
        self._directory = directory
        self._transport = transport

    async def load(self) -> ImportedCatalogue:
        overrides = _read_file(self._directory / "overrides.yaml", CatalogueOverrides)
        extras = _read_file(self._directory / "extra.yaml", _ExtraFile)
        return build_catalogue(
            await self._download(), overrides=overrides, extras=extras.exercises, pin=self._pin
        )

    async def _download(self) -> list[DatasetRow]:
        url = _DATASET_URL.format(commit=self._pin.commit)
        try:
            async with httpx2.AsyncClient(
                transport=self._transport, timeout=_DOWNLOAD_TIMEOUT_SECONDS
            ) as client:
                response = (await client.get(url)).raise_for_status()
            return _ROWS.validate_json(response.content)
        except httpx2.HTTPError as exc:
            raise ExerciseDatasetError(f"{url}: {exc}") from exc
        except ValidationError as exc:
            raise ExerciseDatasetError(
                f"{url}: not the expected shape, {exc.error_count()} errors"
            ) from exc
