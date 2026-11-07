"""The `ai-trainer-evals` entry point and its composition root (ADR-0003, ADR-0034, ticket #429).
It hands the evals the app's own exercise shortlist for the smoke set's live cases: the same
wiring as the app (`EXERCISE_SEARCH_ENABLED` decides whether the catalogue search joins), on the
database `DATABASE_URL` names, which must hold the imported catalogue and, for the search, its
embedded texts. The embedding call is charged to the oldest active admin, as ingestion's is
(ADR-0036 #427): `llm_calls.user_id` is NOT NULL."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.application.exercise_candidates import (
    KnowledgeExerciseSearch,
    SearchedExerciseNames,
)
from ai_trainer.application.ports.exercises import ExerciseNamesPort
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals.cli import main as run_evals
from ai_trainer.llm.evals.exercise_shortlist_smoke import Shortlister
from ai_trainer.llm.evals.smoke import SmokeError
from ai_trainer.main import build_knowledge_search
from ai_trainer.settings import Settings

type SessionFactory = Callable[[], AsyncSession]
# The measure a typed name is first matched against, as `find_exercise` does.
_MEASURE = "load_reps"


async def _acting_admin(session: AsyncSession, settings: Settings) -> UUID:
    emails = [email.lower() for email in settings.admin_emails]
    found = await session.scalar(
        text(
            "SELECT id FROM users WHERE status = 'active' AND lower(email) = ANY(:emails)"
            " ORDER BY created_at LIMIT 1"
        ),
        {"emails": emails},
    )
    if found is None:
        raise SmokeError("No active admin in ADMIN_EMAILS to act for the search; nothing was run")
    return UUID(str(found))


def _names(
    settings: Settings, factory: SessionFactory, http: httpx2.AsyncClient
) -> ExerciseNamesPort:
    exercises = SqlAlchemyExerciseRepository(factory)
    if not settings.exercise_search_enabled:
        return exercises
    return SearchedExerciseNames(
        exercises,
        KnowledgeExerciseSearch(
            build_knowledge_search(
                settings,
                factory,
                default_sport_registry(),
                calls_repository=SqlAlchemyLlmCallsRepository(factory),
                http_client=http,
            )
        ),
        exercises,
        timeout_seconds=settings.exercise_search_timeout_seconds,
    )


@asynccontextmanager
async def live_shortlister(
    settings: Settings,
    *,
    factory: SessionFactory | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[Shortlister]:
    """The shortlist the app would build for a typed name, for the acting admin. `factory` and
    `transport` are for tests; otherwise the database `DATABASE_URL` names is opened and closed
    here and the embedding calls go to OpenRouter."""
    engine = None if factory is not None else build_engine(str(settings.database_url))
    try:
        sessions = factory or build_session_factory(engine)  # type: ignore[arg-type]  # engine is set when factory is None
        async with httpx2.AsyncClient(
            timeout=settings.knowledge_query_timeout_seconds, transport=transport
        ) as http:
            async with sessions() as session:
                user_id = await _acting_admin(session, settings)
            names = _names(settings, sessions, http)

            async def shortlist(name: str) -> Sequence[ExerciseCandidate]:
                return await names.shortlist(
                    user_id,
                    sport_id=GYM_SPORT_ID,
                    name=name,
                    measure=_MEASURE,
                    floor=settings.exercise_candidate_floor,
                    limit=settings.exercise_shortlist_size,
                )

            yield shortlist
    finally:
        if engine is not None:
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """`ai-trainer-evals`: the evals CLI with the live shortlist wired in."""
    settings = Settings()
    return run_evals(argv, live_shortlister=lambda: live_shortlister(settings))
