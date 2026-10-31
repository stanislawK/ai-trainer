"""`ai-trainer import-exercises` against the real Postgres (ADR-0031, ticket #375). The CLI opens
its own engine and commits, so this writes real rows under `zz-cli-` refs and removes them after."""

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text

from ai_trainer import cli
from ai_trainer.adapters.db import build_engine
from ai_trainer.settings import Settings
from tests.exercise_support import (
    SAMPLE,
    SAMPLE_CATALOGUE_DIR,
    SAMPLE_EXERCISES,
    dataset_transport,
)

_REFS = "source_ref LIKE 'zz-cli-%'"


def _renamed(content: str) -> str:
    """The sample under refs and names of its own, so tests that roll back never wait on it."""
    return content.replace("zz-test-", "zz-cli-").replace("Zz Extra", "Zz Cli Extra")


@pytest.fixture
async def _clean_rows() -> AsyncIterator[None]:
    yield
    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        await connection.execute(text(f"DELETE FROM exercises WHERE {_REFS}"))
    await engine.dispose()


@pytest.fixture
def directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("overrides.yaml", "extra.yaml"):
        (tmp_path / name).write_text(_renamed((SAMPLE_CATALOGUE_DIR / name).read_text()))
    monkeypatch.setenv("EXERCISE_CATALOGUE_DIR", str(tmp_path))
    return tmp_path


async def _versions() -> list[str]:
    engine = build_engine(str(Settings().database_url))
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                f"SELECT source_ref, xmin::text, ctid::text FROM exercises WHERE {_REFS} ORDER BY 1"
            )
        )
        found = [" ".join(row) for row in rows]
    await engine.dispose()
    return found


def _body() -> bytes:
    return _renamed(SAMPLE.read_text()).encode()


@pytest.mark.usefixtures("_clean_rows", "directory")
async def test_main_imports_prints_the_outcome_line_and_a_second_run_writes_nothing() -> None:
    first, second = io.StringIO(), io.StringIO()

    first_code = await cli.run(
        ["import-exercises"], output=first, dataset_transport=dataset_transport(_body())
    )
    versions = await _versions()
    second_code = await cli.run(
        ["import-exercises"], output=second, dataset_transport=dataset_transport(_body())
    )

    assert (first_code, second_code) == (0, 0)
    assert first.getvalue() == (
        f"import-exercises: result=ok exercises={SAMPLE_EXERCISES} inserted={SAMPLE_EXERCISES} "
        f"updated=0 unchanged=0 texts_written={2 * (SAMPLE_EXERCISES - 1) + 1} skipped=1\n"
    )
    assert second.getvalue() == (
        f"import-exercises: result=ok exercises={SAMPLE_EXERCISES} inserted=0 "
        f"updated=0 unchanged={SAMPLE_EXERCISES} texts_written=0 skipped=1\n"
    )
    assert len(versions) == SAMPLE_EXERCISES
    assert await _versions() == versions


@pytest.mark.usefixtures("_clean_rows", "directory")
async def test_failed_download_changes_nothing_and_exits_non_zero() -> None:
    await cli.run(
        ["import-exercises"], output=io.StringIO(), dataset_transport=dataset_transport(_body())
    )
    before = await _versions()
    output = io.StringIO()

    code = await cli.run(
        ["import-exercises"],
        output=output,
        dataset_transport=dataset_transport(b"oops", status=503),
    )

    assert code == 1
    assert output.getvalue().startswith("import-exercises: result=failed reason=download")
    assert before
    assert await _versions() == before


@pytest.mark.usefixtures("_clean_rows")
async def test_invalid_catalogue_files_change_nothing_and_exit_non_zero(directory: Path) -> None:
    (directory / "overrides.yaml").write_text("skip: [not-in-the-dataset]\n")
    output = io.StringIO()

    code = await cli.run(
        ["import-exercises"], output=output, dataset_transport=dataset_transport(_body())
    )

    assert code == 1
    assert output.getvalue().startswith("import-exercises: result=failed reason=catalogue")
    assert "not-in-the-dataset" in output.getvalue()
    assert await _versions() == []
