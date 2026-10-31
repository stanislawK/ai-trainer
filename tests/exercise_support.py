"""The recorded dataset sample and the catalogue source tests read it through (ADR-0031). Its rows
use `zz-test-` refs, which no real dataset row has, so a test's counts hold in a database that
also holds a real import."""

from datetime import date
from pathlib import Path

import httpx2

from ai_trainer.adapters.exercises_dataset import PinnedDatasetCatalogueSource

FIXTURES = Path("tests/fixtures")
SAMPLE = FIXTURES / "exercises_dataset_sample.json"
SAMPLE_CATALOGUE_DIR = FIXTURES / "exercise_catalogue"
COMMIT = "7455efae41b330c265e7cd4b78dfa848e7ce5ebd"
FETCHED_ON = date(2026, 10, 31)
# Nine dataset rows less one skip, plus one supplement entry.
SAMPLE_EXERCISES = 9


def dataset_transport(body: bytes, *, status: int = 200) -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda _request: httpx2.Response(status, content=body))


def sample_source(
    transport: httpx2.AsyncBaseTransport | None = None,
    *,
    directory: Path = SAMPLE_CATALOGUE_DIR,
) -> PinnedDatasetCatalogueSource:
    return PinnedDatasetCatalogueSource(
        commit=COMMIT,
        fetched_on=FETCHED_ON,
        directory=directory,
        transport=transport or dataset_transport(SAMPLE.read_bytes()),
    )
