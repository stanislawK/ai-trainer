"""The composition root adds the catalogue search to the gym's shortlist only when asked to
(`EXERCISE_SEARCH_ENABLED`, ADR-0031, ticket #429), so the default makes no embedding call."""

from typing import Any, cast

import httpx2
import pytest

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.application.exercise_candidates import SearchedExerciseNames
from ai_trainer.application.exercise_resolution import ResolveExercises
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.main import build_draft_settlers
from ai_trainer.settings import Settings
from tests.chat_support import ScriptedConversation
from tests.unit.test_llm_gateway import FakeLlmCallsRepository


@pytest.fixture(autouse=True)
def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "test/judge-model")


async def _names(settings: Settings) -> object:
    async with httpx2.AsyncClient() as client:
        settlers = build_draft_settlers(
            settings,
            cast(Any, lambda: None),
            ScriptedConversation(),
            cast(Any, object()),
            sports=default_sport_registry(),
            calls_repository=FakeLlmCallsRepository(),
            http_client=client,
        )
    gym = settlers["gym"]
    assert isinstance(gym, ResolveExercises)
    return gym._names


def test_the_search_is_off_by_default() -> None:
    assert Settings(_env_file=None).exercise_search_enabled is False


async def test_the_default_shortlist_is_today_s_repository() -> None:
    names = await _names(Settings(_env_file=None))

    assert isinstance(names, SqlAlchemyExerciseRepository)


async def test_enabling_the_search_wraps_the_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXERCISE_SEARCH_ENABLED", "true")

    names = await _names(Settings(_env_file=None))

    assert isinstance(names, SearchedExerciseNames)
