"""Reading the pinned dataset and the committed files (ADR-0031, ticket #375). No network: every
response comes from a mock transport."""

import json
from pathlib import Path

import httpx2
import pytest

from ai_trainer.domain.exercises import ExerciseCatalogueError, ExerciseDatasetError
from tests.exercise_support import (
    COMMIT,
    SAMPLE,
    SAMPLE_CATALOGUE_DIR,
    SAMPLE_EXERCISES,
    dataset_transport,
    sample_source,
)


async def test_load_reads_only_the_pinned_json_and_never_media() -> None:
    requested: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        return httpx2.Response(200, content=SAMPLE.read_bytes())

    built = await sample_source(httpx2.MockTransport(respond)).load()

    assert requested == [
        "https://raw.githubusercontent.com/hasaneyldrm/exercises-dataset/"
        f"{COMMIT}/data/exercises.json"
    ]
    assert len(built.exercises) == SAMPLE_EXERCISES
    assert built.skipped == 1


async def test_a_server_error_is_a_dataset_error() -> None:
    with pytest.raises(ExerciseDatasetError, match="500"):
        await sample_source(dataset_transport(b"oops", status=500)).load()


async def test_a_timeout_is_a_dataset_error() -> None:
    def time_out(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    with pytest.raises(ExerciseDatasetError):
        await sample_source(httpx2.MockTransport(time_out)).load()


async def test_invalid_json_is_a_dataset_error() -> None:
    with pytest.raises(ExerciseDatasetError):
        await sample_source(dataset_transport(b"[{")).load()


async def test_a_body_of_the_wrong_shape_is_a_dataset_error() -> None:
    record = json.loads(SAMPLE.read_text())[0]
    del record["instructions"]

    with pytest.raises(ExerciseDatasetError):
        await sample_source(dataset_transport(json.dumps([record]).encode())).load()


async def test_an_empty_dataset_is_a_dataset_error() -> None:
    with pytest.raises(ExerciseDatasetError):
        await sample_source(dataset_transport(b"[]")).load()


async def test_invalid_overrides_are_a_catalogue_error(tmp_path: Path) -> None:
    (tmp_path / "extra.yaml").write_text((SAMPLE_CATALOGUE_DIR / "extra.yaml").read_text())
    (tmp_path / "overrides.yaml").write_text("nonsense: true\n")

    with pytest.raises(ExerciseCatalogueError, match=r"overrides\.yaml"):
        await sample_source(directory=tmp_path).load()


async def test_a_missing_file_is_a_catalogue_error_before_any_download(tmp_path: Path) -> None:
    requested: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        return httpx2.Response(200, content=SAMPLE.read_bytes())

    with pytest.raises(ExerciseCatalogueError, match=r"overrides\.yaml|extra\.yaml"):
        await sample_source(httpx2.MockTransport(respond), directory=tmp_path).load()

    assert requested == []


async def test_a_file_that_is_not_utf8_is_a_catalogue_error(tmp_path: Path) -> None:
    (tmp_path / "overrides.yaml").write_bytes(b"skip: [\xff]\n")

    with pytest.raises(ExerciseCatalogueError, match=r"overrides\.yaml"):
        await sample_source(directory=tmp_path).load()


async def test_yaml_that_does_not_parse_is_a_catalogue_error(tmp_path: Path) -> None:
    (tmp_path / "extra.yaml").write_text((SAMPLE_CATALOGUE_DIR / "extra.yaml").read_text())
    (tmp_path / "overrides.yaml").write_text("skip: [unclosed\n")

    with pytest.raises(ExerciseCatalogueError, match=r"overrides\.yaml"):
        await sample_source(directory=tmp_path).load()
