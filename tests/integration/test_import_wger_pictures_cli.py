"""`ai-trainer import-wger-pictures` wired end to end (ADR-0031 *First media source*, #552): real
PostgreSQL, an in-memory media bucket, and wger replayed from `tests/fixtures/wger/` through a
mock transport, so nothing leaves the machine. The command commits, so the catalogue exercises it
matches are real rows in their own sport, and every row it writes is removed after."""

import hashlib
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_trainer import cli
from ai_trainer.adapters.db import build_engine
from ai_trainer.cli_media_wger import IMPORT_WGER_PICTURES, WgerHooks
from ai_trainer.settings import Settings
from tests.picture_support import InMemoryBucket
from tests.wger_support import BENCH_PRESS_FILE, VACUUM_FILE, FakeWgerSite

_SOURCE = "zz-wger-cli"
_SHAS = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (BENCH_PRESS_FILE, VACUUM_FILE)]


@dataclass(frozen=True)
class Catalogue:
    sport: str
    bench: UUID
    vacuum: UUID


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


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = build_engine(str(Settings().database_url))
    await _clean(engine)
    try:
        yield engine
    finally:
        await _clean(engine)
        await engine.dispose()


@pytest.fixture
async def catalogue(engine: AsyncEngine) -> Catalogue:
    sport = f"zz-wger-{uuid4()}"
    ids: dict[str, UUID] = {}
    async with engine.begin() as connection:
        # Exactly wger's English names; "Biceps Curl With Cable" has no catalogue row.
        for name in ("Bench Press Narrow Grip", "Stomach Vacuum"):
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
    return Catalogue(sport, ids["Bench Press Narrow Grip"], ids["Stomach Vacuum"])


async def _run(
    catalogue: Catalogue,
    bucket: InMemoryBucket,
    *,
    site: FakeWgerSite | None = None,
    args: tuple[str, ...] = (),
) -> tuple[int, str]:
    output = io.StringIO()
    hooks = WgerHooks(
        transport=(site or FakeWgerSite()).transport(), storage=bucket, sport_id=catalogue.sport
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
