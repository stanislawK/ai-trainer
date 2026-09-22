"""Regenerates the committed OpenAPI snapshot from the FastAPI app (ADR-0013).

Run with `uv run python scripts/generate_openapi.py` (or `make openapi`).
Never hand-edit `docs/api/openapi.json`.
"""

import json
from pathlib import Path

from fastapi import FastAPI

from ai_trainer.main import create_app
from ai_trainer.settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = REPO_ROOT / "docs" / "api" / "openapi.json"


def render_openapi_snapshot(app: FastAPI) -> str:
    """Serializes the app's OpenAPI schema with a stable key order and a trailing newline."""
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def write_openapi_snapshot(app: FastAPI, output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_openapi_snapshot(app))


def main(output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    write_openapi_snapshot(create_app(Settings()), output_path)


if __name__ == "__main__":
    main()  # pragma: no cover -- only runs via direct script execution, not import
