"""The smoke set's live shortlist on PostgreSQL (ADR-0034, ticket #429): the app's own wiring on
the imported catalogue, for the oldest active admin, with the catalogue search only when
`EXERCISE_SEARCH_ENABLED` asks for it. The embedding call itself is not made here."""

from collections.abc import Callable

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.evals_main import live_shortlister
from ai_trainer.llm.evals.smoke import SmokeError
from ai_trainer.settings import Settings
from tests.exercise_support import sample_source

SessionFactory = Callable[[], AsyncSession]


@pytest.fixture(autouse=True)
def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "test/judge-model")
    monkeypatch.setenv("ADMIN_EMAILS", "live-admin@example.com")


async def _user(factory: SessionFactory, email: str, status: UserStatus) -> None:
    await SqlAlchemyUsersRepository(factory).create(
        NewUser(sub=email, email=email, name=None, locale="en", status=status)
    )


async def test_the_shortlist_is_the_apps_for_the_imported_catalogue(
    db_session_factory: SessionFactory,
) -> None:
    await import_catalogue(
        source=sample_source(), exercises=SqlAlchemyExerciseRepository(db_session_factory)
    )
    await _user(db_session_factory, "live-admin@example.com", UserStatus.ACTIVE)

    async with live_shortlister(Settings(), factory=db_session_factory) as shortlist:
        found = await shortlist("Bench Press")

    assert found  # the sample catalogue holds a bench press
    assert len(found) <= Settings().exercise_shortlist_size


class _Upstream:
    """An embeddings upstream that is down. Records every request it is sent."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.transport = httpx2.MockTransport(self._down)

    def _down(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(500)


async def test_the_search_is_tried_when_enabled_and_the_shortlist_falls_back_when_it_fails(
    db_session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await import_catalogue(
        source=sample_source(), exercises=SqlAlchemyExerciseRepository(db_session_factory)
    )
    await _user(db_session_factory, "live-admin@example.com", UserStatus.ACTIVE)
    monkeypatch.setenv("EXERCISE_SEARCH_ENABLED", "true")
    upstream = _Upstream()

    async with live_shortlister(
        Settings(), factory=db_session_factory, transport=upstream.transport
    ) as shortlist:
        found = await shortlist("Bench Press")

    assert upstream.requests  # the embedding call was attempted
    assert found  # and today's shortlist stood


async def test_the_search_is_not_tried_when_disabled(db_session_factory: SessionFactory) -> None:
    await _user(db_session_factory, "live-admin@example.com", UserStatus.ACTIVE)
    upstream = _Upstream()

    async with live_shortlister(
        Settings(), factory=db_session_factory, transport=upstream.transport
    ) as shortlist:
        await shortlist("Bench Press")

    assert upstream.requests == []


async def test_it_opens_and_closes_its_own_connection_when_given_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nobody has this address, so the run stops at the admin lookup: a read, nothing written.
    monkeypatch.setenv("ADMIN_EMAILS", "nobody-has-this@example.invalid")

    with pytest.raises(SmokeError, match="admin"):
        async with live_shortlister(Settings()):
            pass


async def test_with_no_admin_emails_set_nothing_is_run(
    db_session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "")

    with pytest.raises(SmokeError, match="ADMIN_EMAILS"):
        async with live_shortlister(Settings(), factory=db_session_factory):
            pass


async def test_without_an_active_admin_nothing_is_run(db_session_factory: SessionFactory) -> None:
    await _user(db_session_factory, "live-admin@example.com", UserStatus.PENDING)

    with pytest.raises(SmokeError, match="admin"):
        async with live_shortlister(Settings(), factory=db_session_factory):
            pass
