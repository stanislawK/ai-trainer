import asyncio

import pytest
from pydantic_ai import models


def test_asyncio_mode_is_auto(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.getini("asyncio_mode") == "auto"


async def test_async_test_runs_without_marker() -> None:
    assert asyncio.get_running_loop().is_running()


def test_real_model_requests_are_refused() -> None:
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS is False"):
        models.check_allow_model_requests()
