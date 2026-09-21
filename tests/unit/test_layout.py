import importlib

import pytest

PACKAGES = [
    "ai_trainer",
    "ai_trainer.domain",
    "ai_trainer.application",
    "ai_trainer.application.ports",
    "ai_trainer.adapters",
    "ai_trainer.llm",
    "ai_trainer.mcp",
    "ai_trainer.knowledge",
    "ai_trainer.web",
    "ai_trainer.main",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_is_importable(name: str) -> None:
    importlib.import_module(name)
