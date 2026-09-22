from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from importlinter.cli import lint_imports

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "ai_trainer"
FIXTURE_NAME = "_boundary_check_fixture.py"


@pytest.fixture(autouse=True)
def _no_stray_fixture_files() -> None:
    """Sweeps a leftover fixture file from a hard-killed previous run before it can
    mask or fake a result here (self-healing since `finally` alone can't survive a
    SIGKILL)."""
    for stray in SRC_ROOT.glob(f"*/{FIXTURE_NAME}"):
        stray.unlink()


@pytest.fixture
def temp_module() -> Iterator[Callable[[str, str], None]]:
    """Writes a throwaway module into `src/ai_trainer`, then removes it (ADR-0013)."""
    created: list[Path] = []

    def _create(package: str, contents: str) -> None:
        path = SRC_ROOT / package / FIXTURE_NAME
        path.write_text(contents)
        created.append(path)

    try:
        yield _create
    finally:
        for path in created:
            path.unlink(missing_ok=True)


def test_layers_contract_passes_on_the_current_tree() -> None:
    assert lint_imports(no_cache=True, no_logo=True) == 0


def test_application_importing_adapters_fails(
    temp_module: Callable[[str, str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    temp_module("application", "from ai_trainer import adapters  # noqa: F401\n")

    exit_code = lint_imports(no_cache=True, no_logo=True)

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "ai_trainer.application" in output
    assert "ai_trainer.adapters" in output


def test_domain_importing_application_fails(
    temp_module: Callable[[str, str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    temp_module("domain", "from ai_trainer import application  # noqa: F401\n")

    exit_code = lint_imports(no_cache=True, no_logo=True)

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "ai_trainer.domain" in output
    assert "ai_trainer.application" in output


def test_domain_importing_a_non_pydantic_dependency_fails(
    temp_module: Callable[[str, str], None], capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-0003 invariant 1: domain imports only the standard library and Pydantic."""
    temp_module("domain", "from fastapi import APIRouter  # noqa: F401\n")

    exit_code = lint_imports(no_cache=True, no_logo=True)

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "ai_trainer.domain" in output
    assert "fastapi" in output
