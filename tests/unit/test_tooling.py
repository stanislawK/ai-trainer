import asyncio

import pytest
from pydantic_ai import models


def test_asyncio_mode_is_auto(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.getini("asyncio_mode") == "auto"


def test_testpaths_excludes_e2e_by_default(pytestconfig: pytest.Config) -> None:
    """`tests/e2e` needs the running compose stack (ADR-0013); a bare `uv run pytest` must
    never try to collect it, only `make e2e` / `uv run pytest tests/e2e` explicitly."""
    assert pytestconfig.getini("testpaths") == ["tests/unit", "tests/integration"]


async def test_async_test_runs_without_marker() -> None:
    assert asyncio.get_running_loop().is_running()


def test_real_model_requests_are_refused() -> None:
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS is False"):
        models.check_allow_model_requests()
