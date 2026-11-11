"""`ai-trainer import-wger-pictures` wired end to end (ADR-0031 *First media source*, #552, #555):
real PostgreSQL, an in-memory media bucket, wger replayed from `tests/fixtures/wger/` through a
mock transport, and Jev faked behind `DecisionGatewayPort` (or behind the real gateway as a fake
`DecisionModel`), so nothing leaves the machine. The command commits, so the admin and the
catalogue exercises it matches are real rows, in their own sport, and every row it writes is
removed after."""

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import BaseModel
from pydantic_ai import models
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_trainer import cli
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.cli_media_wger import IMPORT_WGER_PICTURES, WgerHooks
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.exercise_shortlist import EXERCISE_SHORTLIST_ID, EXERCISE_SHORTLIST_VERSION
from ai_trainer.settings import Settings
from tests.picture_support import InMemoryBucket
from tests.wger_support import BENCH_PRESS_FILE, VACUUM_FILE, FakeWgerSite

_SOURCE = "zz-wger-cli"
_ADMIN_EMAIL = "zz-wger-cli-admin@example.com"
_PENDING_EMAIL = "zz-wger-cli-pending@example.com"
_SHAS = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (BENCH_PRESS_FILE, VACUUM_FILE)]


@dataclass(frozen=True)
class Catalogue:
    sport: str
    bench: UUID
    vacuum: UUID
    close_grip: UUID


@dataclass
class FakeDecisions:
    """A `DecisionGatewayPort` that picks, for each wger name, the option `picks` names at 0.97,
    and "none" for any other name. It records each call's user and state."""

    picks: dict[str, UUID] = field(default_factory=dict)
    calls: list[tuple[UUID, str, list[str]]] = field(default_factory=list)

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        options = [
            option["const"]
            for option in output_type.model_json_schema()["properties"]["exercise"]["anyOf"]
        ]
        self.calls.append((user_id, state, options))
        option = str(self.picks[state]) if state in self.picks else "none"
        return DecisionResult(
            output=output_type.model_validate({"exercise": option}),
            outcome=LlmCallOutcome.SUCCESS,
            probabilities={"exercise": {option: 0.97}},
        )


async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM exercise_media WHERE asset_id IN "
                "(SELECT id FROM media_assets WHERE sha256 = ANY(:shas))"
            ),
            {"shas": _SHAS},
        )
        await connection.execute(
            text("DELETE FROM media_assets WHERE sha256 = ANY(:shas)"), {"shas": _SHAS}
        )
        await connection.execute(text("DELETE FROM exercises WHERE source = :s"), {"s": _SOURCE})
        # Their `llm_calls` rows go by cascade.
        await connection.execute(
            text("DELETE FROM users WHERE email = ANY(:emails)"),
            {"emails": [_ADMIN_EMAIL, _PENDING_EMAIL]},
        )


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = build_engine(str(Settings().database_url))
    await _clean(engine)
    try:
        yield engine
    finally:
        await _clean(engine)
        await engine.dispose()


async def _user(engine: AsyncEngine, email: str, status: UserStatus) -> UUID:
    user = await SqlAlchemyUsersRepository(build_session_factory(engine)).create(
        NewUser(sub=f"zz-wger-cli-{uuid4()}", email=email, name=None, locale="en", status=status)
    )
    return user.id


@pytest.fixture
async def admin(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> UUID:
    monkeypatch.setenv("ADMIN_EMAILS", _ADMIN_EMAIL)
    return await _user(engine, _ADMIN_EMAIL, UserStatus.ACTIVE)


@pytest.fixture
async def catalogue(engine: AsyncEngine, admin: UUID) -> Catalogue:
    sport = f"zz-wger-{uuid4()}"
    ids: dict[str, UUID] = {}
    async with engine.begin() as connection:
        # Two of wger's English names exactly, and a near name for the bench press; "Biceps Curl
        # With Cable" has no catalogue row near it.
        for name in ("Bench Press Narrow Grip", "Stomach Vacuum", "Close-Grip Bench Press"):
            ids[name] = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO exercises (id, sport_id, source, source_ref, name, "
                    "default_measure) VALUES (:id, :sport, :source, :ref, :name, 'reps')"
                ),
                {
                    "id": ids[name],
                    "sport": sport,
                    "source": _SOURCE,
                    "ref": f"{_SOURCE}-{ids[name]}",
                    "name": name,
                },
            )
    return Catalogue(
        sport, ids["Bench Press Narrow Grip"], ids["Stomach Vacuum"], ids["Close-Grip Bench Press"]
    )


def _exact(catalogue: Catalogue) -> FakeDecisions:
    """Jev picking each exact catalogue name."""
    return FakeDecisions(
        picks={"Bench Press Narrow Grip": catalogue.bench, "Stomach Vacuum": catalogue.vacuum}
    )


async def _run(
    catalogue: Catalogue,
    bucket: InMemoryBucket,
    *,
    site: FakeWgerSite | None = None,
    args: tuple[str, ...] = (),
    decisions: FakeDecisions | OpenRouterDecisionGateway | None = None,
) -> tuple[int, str]:
    output = io.StringIO()
    hooks = WgerHooks(
        transport=(site or FakeWgerSite()).transport(),
        storage=bucket,
        sport_id=catalogue.sport,
        decisions=decisions or _exact(catalogue),
    )
    code = await cli.run([IMPORT_WGER_PICTURES, *args], output=output, wger=hooks)
    return code, output.getvalue()


async def _media(engine: AsyncEngine, sport: str) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT em.exercise_id, em.status, a.* FROM exercise_media em "
                "JOIN media_assets a ON a.id = em.asset_id "
                "JOIN exercises e ON e.id = em.exercise_id WHERE e.sport_id = :s "
                "ORDER BY a.source_ref"
            ),
            {"s": sport},
        )
        return [dict(row) for row in rows.mappings()]


async def test_replay_stores_authored_matched_images(
    engine: AsyncEngine, catalogue: Catalogue
) -> None:
    bucket = InMemoryBucket()

    code, output = await _run(catalogue, bucket)

    assert code == 0
    lines = output.splitlines()
    assert "import-wger-pictures: skipped reason=no_author image=3" in lines
    assert "import-wger-pictures: unmatched image=19" in lines
    assert lines[-1] == "import-wger-pictures: result=done stored=2 skipped=1 unmatched=1"
    bench, vacuum = await _media(engine, catalogue.sport)
    assert (bench["exercise_id"], bench["status"]) == (catalogue.bench, "suggested")
    assert bench["source_ref"] == "https://wger.de/api/v2/exerciseimage/15/"
    assert bench["license"] == "CC-BY-SA 3"
    assert bench["attribution"] == (
        "Everkinetic · CC-BY-SA 3 (https://creativecommons.org/licenses/by-sa/3.0/deed.en) "
        "— via wger.de"
    )
    assert (vacuum["exercise_id"], vacuum["status"]) == (catalogue.vacuum, "suggested")
    assert vacuum["source_ref"] == "https://wger.de/api/v2/exerciseimage/724/#ai-generated"
    assert vacuum["license"] == "CC-BY-SA 4"
    # Byte for byte, under the key of wger's own file's sha256.
    assert bucket.objects == {
        f"exercises/{catalogue.bench}/{_SHAS[0]}.png": BENCH_PRESS_FILE.read_bytes(),
        f"exercises/{catalogue.vacuum}/{_SHAS[1]}.jpg": VACUUM_FILE.read_bytes(),
    }
    assert all(
        (row["kind"], row["rights"], row["delivery"]) == ("image", "licensed", "stored")
        for row in (bench, vacuum)
    )


async def test_second_run_writes_nothing(engine: AsyncEngine, catalogue: Catalogue) -> None:
    bucket = InMemoryBucket()
    await _run(catalogue, bucket)
    rows = await _media(engine, catalogue.sport)
    objects = dict(bucket.objects)

    code, output = await _run(catalogue, bucket)

    assert code == 0
    assert "import-wger-pictures: skipped reason=known_sha256 image=15" in output.splitlines()
    assert output.splitlines()[-1] == (
        "import-wger-pictures: result=done stored=0 skipped=3 unmatched=1"
    )
    assert await _media(engine, catalogue.sport) == rows
    assert bucket.objects == objects


async def test_removed_picture_is_not_imported_again(
    engine: AsyncEngine, catalogue: Catalogue
) -> None:
    await _run(catalogue, InMemoryBucket())
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE exercise_media SET status = 'removed' WHERE exercise_id = :id"),
            {"id": catalogue.bench},
        )
    bucket = InMemoryBucket()

    code, output = await _run(catalogue, bucket)

    assert code == 0
    assert "import-wger-pictures: skipped reason=known_sha256 image=15" in output.splitlines()
    assert bucket.objects == {}
    statuses = [str(row["status"]) for row in await _media(engine, catalogue.sport)]
    assert sorted(statuses) == ["removed", "suggested"]


async def test_limit_reads_only_the_first_images(engine: AsyncEngine, catalogue: Catalogue) -> None:
    code, output = await _run(catalogue, InMemoryBucket(), args=("--limit", "2"))

    assert code == 0
    assert output.splitlines()[-1] == (
        "import-wger-pictures: result=done stored=1 skipped=1 unmatched=0"
    )


async def test_wger_error_mid_run_exits_non_zero(engine: AsyncEngine, catalogue: Catalogue) -> None:
    site = FakeWgerSite(failing={"/api/v2/exerciseimage/?offset=2": "server_error"})
    bucket = InMemoryBucket()

    code, output = await _run(catalogue, bucket, site=site)

    assert code == 1
    assert output.splitlines()[-1] == (
        "import-wger-pictures: result=failed reason=wger_error stored=1 skipped=1 unmatched=0"
    )
    rows = await _media(engine, catalogue.sport)
    # No orphan: every object in the bucket has its row, and the reverse.
    assert set(bucket.objects) == {row["storage_key"] for row in rows}


async def test_wger_timeout_exits_non_zero(
    engine: AsyncEngine, catalogue: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WGER_TIMEOUT_SECONDS", "0.05")
    path = "/media/exercise-images/2673/e947b431-e4c4-48fa-bb21-de293734cce8.jpg"
    bucket = InMemoryBucket()

    code, output = await _run(catalogue, bucket, site=FakeWgerSite(failing={path: "timeout"}))

    assert code == 1
    assert output.splitlines()[-1] == (
        "import-wger-pictures: result=failed reason=wger_timeout stored=1 skipped=1 unmatched=1"
    )
    rows = await _media(engine, catalogue.sport)
    assert set(bucket.objects) == {row["storage_key"] for row in rows}
    assert len(rows) == 1


async def test_report_counts_stored_skipped_unmatched(
    engine: AsyncEngine, catalogue: Catalogue
) -> None:
    code, output = await _run(catalogue, InMemoryBucket())

    stored = [line for line in output.splitlines() if " stored image=" in line]
    assert code == 0
    assert stored == [
        f"import-wger-pictures: stored image=15 exercise={catalogue.bench} asset={asset}"
        for asset in [stored[0].rsplit("=", 1)[1]]
    ] + [
        f"import-wger-pictures: stored image=724 exercise={catalogue.vacuum} asset={asset}"
        for asset in [stored[1].rsplit("=", 1)[1]]
    ]
    # The run never prints a storage key, a URL or wger's author.
    assert "exercises/" not in output
    assert "http" not in output
    assert "Everkinetic" not in output


async def test_cli_links_the_exercise_jev_picked(engine: AsyncEngine, catalogue: Catalogue) -> None:
    """ "Bench Press Narrow Grip" is its own trigram top; Jev picks the close-grip press from the
    shortlist instead, and that is the link (#555)."""
    decisions = FakeDecisions(
        picks={"Bench Press Narrow Grip": catalogue.close_grip, "Stomach Vacuum": catalogue.vacuum}
    )

    code, output = await _run(catalogue, InMemoryBucket(), decisions=decisions)

    assert code == 0
    bench_call = next(call for call in decisions.calls if call[1] == "Bench Press Narrow Grip")
    assert bench_call[2][:2] == [str(catalogue.bench), str(catalogue.close_grip)]
    assert f"import-wger-pictures: stored image=15 exercise={catalogue.close_grip} asset=" in output
    linked = {
        row["source_ref"]: row["exercise_id"] for row in await _media(engine, catalogue.sport)
    }
    assert linked["https://wger.de/api/v2/exerciseimage/15/"] == catalogue.close_grip


async def test_jev_abstaining_leaves_the_image_unmatched(
    engine: AsyncEngine, catalogue: Catalogue
) -> None:
    bucket = InMemoryBucket()

    code, output = await _run(catalogue, bucket, decisions=FakeDecisions())

    assert code == 0
    assert output.splitlines()[-1] == (
        "import-wger-pictures: result=done stored=0 skipped=1 unmatched=3"
    )
    assert bucket.objects == {}
    assert await _media(engine, catalogue.sport) == []


async def _llm_calls(engine: AsyncEngine, user_ids: list[UUID]) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT * FROM llm_calls WHERE user_id = ANY(:ids)"), {"ids": user_ids}
        )
        return [dict(row) for row in rows.mappings()]


@pytest.mark.parametrize(
    ("admin_emails", "args", "reason"),
    [
        (_ADMIN_EMAIL, ("--admin", "zz-wger-cli-someone@example.com"), "not_admin"),
        (f"{_ADMIN_EMAIL},{_PENDING_EMAIL}", ("--admin", _PENDING_EMAIL), "not_admin"),
        (f"{_ADMIN_EMAIL},{_PENDING_EMAIL}", (), "admin_required"),
    ],
    ids=["unlisted", "listed_but_pending", "no_flag_two_admins"],
)
async def test_non_admin_is_refused_before_any_request(
    engine: AsyncEngine,
    catalogue: Catalogue,
    admin: UUID,
    monkeypatch: pytest.MonkeyPatch,
    admin_emails: str,
    args: tuple[str, ...],
    reason: str,
) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", admin_emails)
    pending = await _user(engine, _PENDING_EMAIL, UserStatus.PENDING)
    site = FakeWgerSite()
    decisions = _exact(catalogue)

    code, output = await _run(
        catalogue, InMemoryBucket(), site=site, args=args, decisions=decisions
    )

    assert code == 1
    assert output == f"import-wger-pictures: result=refused reason={reason}\n"
    assert site.requests == []
    assert decisions.calls == []
    assert await _llm_calls(engine, [admin, pending]) == []


class FakeJev(DecisionModel[None]):
    """Jev behind the real gateway, no network: it picks the option `picks` names for a state,
    else "none"."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, picks: dict[str, UUID]) -> None:
        super().__init__()
        self._picks = picks
        self.requests: list[DecisionRequest] = []

    @property
    def model_name(self) -> str:
        return "typesafe/jev-1.13-20260917"

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        self.requests.append(request)
        await asyncio.sleep(0)
        choice = str(self._picks[request.state]) if request.state in self._picks else "none"
        answers: dict[str, DecisionAnswer] = {
            "exercise": ChoiceAnswer(choice=choice, confidence=0.97, probabilities={choice: 0.97})
        }
        return DecisionResponse(
            answers=answers, model_name=self.model_name, usage=RequestUsage(input_tokens=120)
        )


async def test_one_llm_calls_row_per_jev_call_under_the_admin(
    engine: AsyncEngine, catalogue: Catalogue, admin: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"a test reached the network: {request.url}")

    jev = FakeJev({"Bench Press Narrow Grip": catalogue.bench})
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(refuse)) as client:
        gateway = OpenRouterDecisionGateway(
            Settings(),
            SqlAlchemyLlmCallsRepository(build_session_factory(engine)),
            http_client=client,
        )
        with gateway.agent.override(model=jev):
            code, output = await _run(catalogue, InMemoryBucket(), decisions=gateway)

    assert code == 0
    assert "import-wger-pictures: stored image=15 exercise=" in output
    rows = await _llm_calls(engine, [admin])
    assert len(jev.requests) >= 2
    assert len(rows) == len(jev.requests)
    assert {(row["user_id"], row["template_id"], row["template_version"]) for row in rows} == {
        (admin, EXERCISE_SHORTLIST_ID, EXERCISE_SHORTLIST_VERSION)
    }
    # The state is the wger name alone: no email, no user ID.
    states = [str(request.state) for request in jev.requests]
    assert "Bench Press Narrow Grip" in states
    assert all("@" not in state and str(admin) not in state for state in states)


async def test_no_media_bucket_is_refused_before_any_request(
    engine: AsyncEngine, catalogue: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBJECT_STORAGE_MEDIA_BUCKET", "")
    site = FakeWgerSite()
    output = io.StringIO()

    code = await cli.run(
        [IMPORT_WGER_PICTURES],
        output=output,
        wger=WgerHooks(transport=site.transport(), sport_id=catalogue.sport),
    )

    assert code == 1
    assert output.getvalue() == (
        "import-wger-pictures: result=refused reason=storage_not_configured\n"
    )
    assert site.requests == []


@pytest.mark.parametrize("limit", ["0", "-3", "many"], ids=["zero", "negative", "word"])
def test_limit_must_be_a_positive_whole_number(limit: str) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args([IMPORT_WGER_PICTURES, "--limit", limit])
