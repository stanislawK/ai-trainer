import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import PostgresDsn

from ai_trainer.main import create_app
from ai_trainer.settings import Settings
from scripts.generate_openapi import (
    DEFAULT_OUTPUT_PATH,
    REPO_ROOT,
    main,
    render_openapi_snapshot,
    write_openapi_snapshot,
)

DATABASE_URL = "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"


def _build_app() -> FastAPI:
    settings = Settings(_env_file=None, database_url=PostgresDsn(DATABASE_URL))
    return create_app(settings)


def test_render_openapi_snapshot_includes_health_path() -> None:
    snapshot = render_openapi_snapshot(_build_app())

    schema = json.loads(snapshot)
    assert "/health" in schema["paths"]


def test_render_openapi_snapshot_is_deterministic_across_app_instances() -> None:
    first = render_openapi_snapshot(_build_app())
    second = render_openapi_snapshot(_build_app())

    assert first == second


def test_render_openapi_snapshot_has_stable_key_order_and_trailing_newline() -> None:
    snapshot = render_openapi_snapshot(_build_app())

    assert snapshot.endswith("\n")
    schema = json.loads(snapshot)
    assert json.dumps(schema, indent=2, sort_keys=True) + "\n" == snapshot


def test_write_openapi_snapshot_writes_the_rendered_content(tmp_path: Path) -> None:
    app = _build_app()
    output_path = tmp_path / "openapi.json"

    write_openapi_snapshot(app, output_path)

    assert output_path.read_text() == render_openapi_snapshot(app)


def test_write_openapi_snapshot_creates_missing_parent_directories(tmp_path: Path) -> None:
    app = _build_app()
    output_path = tmp_path / "nested" / "dir" / "openapi.json"

    write_openapi_snapshot(app, output_path)

    assert output_path.exists()


def test_default_output_path_is_anchored_to_the_repo_root_not_the_cwd() -> None:
    assert DEFAULT_OUTPUT_PATH == REPO_ROOT / "docs" / "api" / "openapi.json"
    assert DEFAULT_OUTPUT_PATH.is_absolute()
    assert (REPO_ROOT / "pyproject.toml").exists()


def test_main_writes_the_snapshot_from_settings_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    output_path = tmp_path / "openapi.json"

    main(output_path=output_path)

    schema = json.loads(output_path.read_text())
    assert "/health" in schema["paths"]
