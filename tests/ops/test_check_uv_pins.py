from pathlib import Path

import pytest

from scripts import check_uv_pins as pins

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DOCKERFILE = "COPY --from=ghcr.io/astral-sh/uv:{v} /uv /uvx /bin/\n"
CI = """jobs:
  ci:
    steps:
      - uses: astral-sh/setup-uv@abc # v10
        with:
          # a comment
          version: "{v}"
  e2e:
    steps:
      - uses: astral-sh/setup-uv@abc # v10
        with:
          version: "{e2e}"
"""
EVALS = """jobs:
  evals:
    steps:
      - uses: astral-sh/setup-uv@abc # v10
        with:
          version: "{v}"
"""
PYPROJECT = '[build-system]\nrequires = ["uv_build>={v},<0.14.0"]\nbuild-backend = "uv_build"\n'


def _repo(
    tmp_path: Path,
    *,
    docker: str = "0.13.6",
    ci: str = "0.13.6",
    e2e: str = "0.13.6",
    evals: str = "0.13.6",
    backend: str = "0.13.6",
    upper: str = "0.14.0",
) -> Path:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "Dockerfile").write_text(DOCKERFILE.format(v=docker))
    (tmp_path / ".github/workflows/ci.yml").write_text(CI.format(v=ci, e2e=e2e))
    (tmp_path / ".github/workflows/evals.yml").write_text(EVALS.format(v=evals))
    (tmp_path / "pyproject.toml").write_text(PYPROJECT.format(v=backend).replace("0.14.0", upper))
    return tmp_path


def test_matching_pins_pass(tmp_path: Path) -> None:
    assert pins.check(_repo(tmp_path)) == []


def test_real_repo_pins_agree() -> None:
    assert pins.check(REPO_ROOT) == []


def test_real_ci_workflow_pins_uv_in_each_check_job() -> None:
    """`static`, `test`, `ops` and `e2e` each install uv (#356); the `ci` gate installs nothing."""
    versions = pins._setup_uv_versions(pins._code(REPO_ROOT / ".github/workflows/ci.yml"))
    assert len(versions) == 4
    assert None not in versions


@pytest.mark.parametrize(
    ("override", "file"),
    [
        ({"docker": "0.13.5"}, "Dockerfile"),
        ({"evals": "0.12.0"}, ".github/workflows/evals.yml"),
        ({"ci": "0.13.5"}, ".github/workflows/ci.yml"),
        ({"e2e": "0.13.5"}, ".github/workflows/ci.yml"),
        ({"backend": "0.13.5"}, "pyproject.toml"),
        ({"upper": "0.15.0"}, "pyproject.toml"),
    ],
)
def test_one_differing_pin_names_its_file(
    tmp_path: Path, override: dict[str, str], file: str
) -> None:
    problems = pins.check(_repo(tmp_path, **override))

    assert len(problems) == 1
    assert file in problems[0]


def test_missing_pin_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "Dockerfile").write_text("FROM python:3.14\n")

    problems = pins.check(root)

    assert any("Dockerfile" in p and "no uv pin" in p for p in problems)


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert pins.main(["--root", str(_repo(tmp_path))]) == 0
    assert "0.13.6" in capsys.readouterr().out

    (tmp_path / "Dockerfile").write_text(DOCKERFILE.format(v="0.13.0"))
    assert pins.main(["--root", str(tmp_path)]) == 1
    assert "Dockerfile" in capsys.readouterr().err


def test_setup_uv_without_version_and_missing_files_content_are_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".github/workflows/evals.yml").write_text(
        "steps:\n  - uses: astral-sh/setup-uv@abc\n    with:\n      enable-cache: true\n"
        '  - name: next\n    with:\n      version: "0.13.6"\n'
    )
    (root / ".github/workflows/ci.yml").write_text("jobs: {}\n")
    (root / "pyproject.toml").write_text("[build-system]\nrequires = []\n")

    problems = pins.check(root)

    assert any("evals.yml" in p and "no pinned version" in p for p in problems)
    assert any("ci.yml" in p and "no setup-uv step" in p for p in problems)
    assert any("pyproject.toml" in p and "no uv_build" in p for p in problems)


def test_main_reports_unknown_version_without_dockerfile_pin(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "Dockerfile").write_text("FROM python:3.14\n")

    assert pins.main(["--root", str(root)]) == 1
    assert pins._pinned_version(root) == "?"


def test_commented_out_pins_are_ignored(tmp_path: Path) -> None:
    root = _repo(tmp_path, docker="0.13.7", backend="0.13.7")
    dockerfile = root / "Dockerfile"
    dockerfile.write_text("# was ghcr.io/astral-sh/uv:0.13.6\n" + dockerfile.read_text())
    pyproject = root / "pyproject.toml"
    pyproject.write_text('# was "uv_build>=0.13.6,<0.14.0"\n' + pyproject.read_text())

    problems = pins.check(root)

    assert any("Dockerfile" in p for p in problems)
    assert any("pyproject.toml" in p for p in problems)


def test_a_second_pin_in_a_file_is_checked_too(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    dockerfile = root / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + DOCKERFILE.format(v="0.13.7"))

    problems = pins.check(root)

    assert len(problems) == 1
    assert "Dockerfile" in problems[0]


def test_trailing_comment_after_a_workflow_version_is_read(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    evals = root / ".github/workflows/evals.yml"
    evals.write_text(evals.read_text().replace('"0.13.6"', '"0.13.6"  # pinned'))

    assert pins.check(root) == []
