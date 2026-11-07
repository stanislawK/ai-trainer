"""The `ai-trainer-evals` entry point and its composition root (ADR-0003, ADR-0034, ticket #429).
It hands the evals the app's own exercise shortlist for the smoke set's live cases: the same
`build_exercise_names` as the app (`EXERCISE_SEARCH_ENABLED` decides whether the catalogue search
joins), on the database `DATABASE_URL` names, which must hold the imported catalogue and, for the
search, its embedded texts. The embedding call is charged to the oldest active admin, as
ingestion's is (ADR-0036 #427): `llm_calls.user_id` is NOT NULL. Evals are not capped by the
per-user budget (`.claude/rules/llm.md`)."""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals.cli import main as run_evals
from ai_trainer.llm.evals.exercise_shortlist_smoke import Shortlister
from ai_trainer.llm.evals.smoke import SmokeError
from ai_trainer.main import build_exercise_names
from ai_trainer.settings import Settings

type SessionFactory = Callable[[], AsyncSession]


async def _acting_admin(session: AsyncSession, settings: Settings) -> UUID:
    emails = [email.lower() for email in settings.admin_emails]
    if not emails:
        raise SmokeError("ADMIN_EMAILS is empty, so no one can act for the search; nothing was run")
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


@asynccontextmanager
async def _sessions(
    settings: Settings, factory: SessionFactory | None
) -> AsyncIterator[SessionFactory]:
    """The given factory, or the database `DATABASE_URL` names, opened and closed here."""
    if factory is not None:
        yield factory
        return
    engine = build_engine(str(settings.database_url))
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@asynccontextmanager
async def live_shortlister(
    settings: Settings,
    *,
    factory: SessionFactory | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[Shortlister]:
    """The shortlist the app would build for a typed name, for the acting admin. `factory` and
    `transport` are for tests; otherwise the embedding calls go to OpenRouter."""
    async with (
        _sessions(settings, factory) as sessions,
        httpx2.AsyncClient(
            timeout=settings.knowledge_query_timeout_seconds, transport=transport
        ) as http,
    ):
        async with sessions() as session:
            user_id = await _acting_admin(session, settings)
        names = build_exercise_names(
            settings,
            sessions,
            default_sport_registry(),
            calls_repository=SqlAlchemyLlmCallsRepository(sessions),
            http_client=http,
            clock=UtcClock(),
            budget=None,
        )

        async def shortlist(name: str, measure: str) -> Sequence[ExerciseCandidate]:
            return await names.shortlist(
                user_id,
                sport_id=GYM_SPORT_ID,
                name=name,
                measure=measure,
                floor=settings.exercise_candidate_floor,
                limit=settings.exercise_shortlist_size,
            )

        yield shortlist


def main(argv: Sequence[str] | None = None) -> int:
    """`ai-trainer-evals`: the evals CLI with the live shortlist wired in."""
    settings = Settings()
    return run_evals(argv, live_shortlister=lambda: live_shortlister(settings))
