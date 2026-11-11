"""`ai-trainer generate-exercise-picture` wired end to end (ADR-0031 *Picture generation*, #533):
real PostgreSQL, OpenRouter's embeddings and images faked at the HTTP transport, and the brief
writer faked through its port, so nothing leaves the machine. The command opens its own engine
and commits, so the admin and the catalogue exercise are real rows, removed after."""

import base64
import io
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import text

from ai_trainer import cli
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.cli_media import GENERATE_EXERCISE_PICTURE, PictureHooks
from ai_trainer.domain.exercises import CatalogueExercise, ExerciseMeasure, ExerciseText
from ai_trainer.domain.media_generation import (
    BriefFailed,
    NoBrief,
    PictureBrief,
    PictureBriefDeps,
)
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.settings import Settings
from tests.ingestion_support import spread_vector
from tests.picture_support import InMemoryBucket

_ADMIN_EMAIL = "zz-pic-cli-admin@example.com"
_NAME = "Zz Pic Cli Pull-Up"
_PICTURE = b"\xff\xd8\xff-a-jpeg"
_BRIEF = PictureBrief(
    exercise_name=_NAME,
    start_position="Hanging, arms extended",
    end_position="Chest at the bar",
    image_prompt="One person hanging, then chest at the bar. Plain background. No text.",
)


@dataclass
class FakeBriefs:
    seen: list[PictureBriefDeps] = field(default_factory=list)

    async def write(
        self, *, user_id: UUID, deps: PictureBriefDeps
    ) -> PictureBrief | NoBrief | BriefFailed:
        self.seen.append(deps)
        return _BRIEF


@pytest.fixture
async def admin_id(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[UUID]:
    monkeypatch.setenv("ADMIN_EMAILS", _ADMIN_EMAIL)
    engine = build_engine(str(Settings().database_url))
    factory = build_session_factory(engine)
    admin = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=f"zz-pic-cli-{uuid4()}",
            email=_ADMIN_EMAIL,
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    await SqlAlchemyExerciseRepository(factory).upsert_catalogue(
        [
            CatalogueExercise(
                sport_id="gym",
                source="zz-pic-cli",
                source_ref="zz-pic-cli-pull-up",
                name=_NAME,
                equipment="body weight",
                body_part="back",
                target="lats",
                secondary_muscles=(),
                default_measure=ExerciseMeasure.REPS,
                attribution="test",
                texts=(
                    ExerciseText(
                        lang="en", instructions="Hang from a bar, then pull.", steps=("Hang.",)
                    ),
                ),
            )
        ]
    )
    try:
        yield admin.id
    finally:
        async with engine.begin() as connection:
            assets = await connection.execute(
                text(
                    "SELECT em.asset_id FROM exercise_media em "
                    "JOIN exercises e ON e.id = em.exercise_id WHERE e.source = 'zz-pic-cli'"
                )
            )
            asset_ids = [row[0] for row in assets]
            await connection.execute(text("DELETE FROM exercises WHERE source = 'zz-pic-cli'"))
            if asset_ids:
                await connection.execute(
                    text("DELETE FROM media_assets WHERE id = ANY(:ids)"), {"ids": asset_ids}
                )
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": admin.id})
        await engine.dispose()


def _openrouter(images: Callable[[], httpx2.Response], seen: list[str]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/images"):
            return images()
        body = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": body["model"],
                "data": [
                    {"object": "embedding", "index": i, "embedding": list(spread_vector(item))}
                    for i, item in enumerate(body["input"])
                ],
                "usage": {"prompt_tokens": 4, "total_tokens": 4, "cost": 0.0000004},
            },
        )

    return httpx2.MockTransport(handler)


def _picture() -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "data": [{"b64_json": base64.b64encode(_PICTURE).decode(), "media_type": "image/jpeg"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 1290, "cost": 0.0336},
        },
    )


async def _image_rows(admin_id: UUID) -> list[dict[str, object]]:
    engine = build_engine(str(Settings().database_url))
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT model, cost, template_id, outcome FROM llm_calls "
                    "WHERE user_id = :id AND template_id = 'media.picture'"
                ),
                {"id": admin_id},
            )
            return [dict(row) for row in rows.mappings()]
    finally:
        await engine.dispose()


async def test_writes_brief_and_picture(admin_id: UUID, tmp_path: Path) -> None:
    """AC: the command writes the brief and the picture to the output directory."""
    output = io.StringIO()
    briefs = FakeBriefs()
    seen: list[str] = []
    out = tmp_path / "pictures"

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, "zz pic cli pull-up", "--out", str(out)],
        output=output,
        picture=PictureHooks(transport=_openrouter(_picture, seen), brief_writer=briefs),
    )

    assert code == 0
    brief_path = out / "zz-pic-cli-pull-up.brief.json"
    picture_path = out / "zz-pic-cli-pull-up.jpg"
    assert PictureBrief.model_validate_json(brief_path.read_text()) == _BRIEF
    assert picture_path.read_bytes() == _PICTURE
    assert output.getvalue() == (
        f"generate-exercise-picture: result=written brief={brief_path} picture={picture_path} "
        "spent_usd=0.0336004\n"
    )
    [deps] = briefs.seen
    assert deps.exercise_name == _NAME
    assert deps.description is not None
    assert "Hang from a bar, then pull." in deps.description
    assert seen[-1] == "/api/v1/images"
    [row] = await _image_rows(admin_id)
    assert row["model"] == Settings().image_generation_model
    assert row["template_id"] == "media.picture"


async def test_provider_error_exits_non_zero_and_writes_nothing(
    admin_id: UUID, tmp_path: Path
) -> None:
    """AC: a provider error exits non-zero and writes no file."""
    output = io.StringIO()
    out = tmp_path / "pictures"

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--out", str(out), "--admin", _ADMIN_EMAIL],
        output=output,
        picture=PictureHooks(
            transport=_openrouter(lambda: httpx2.Response(502, json={"error": {}}), []),
            brief_writer=FakeBriefs(),
        ),
    )

    assert code == 1
    assert not out.exists()
    assert output.getvalue() == (
        "generate-exercise-picture: result=failed step=image reason=error spent_usd=unknown\n"
    )
    [row] = await _image_rows(admin_id)
    assert row["outcome"] == "error"


async def test_stops_before_the_image_call_over_the_cap(
    admin_id: UUID, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: a run stops before the call when the cap would be exceeded: the cap is one picture's
    price, and the search has already spent some of it."""
    monkeypatch.setenv("IMAGE_GENERATION_PRICE_USD", "0.0336")
    monkeypatch.setenv("IMAGE_GENERATION_SPEND_CAP_USD", "0.0336")
    output = io.StringIO()
    seen: list[str] = []
    out = tmp_path / "pictures"

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--out", str(out)],
        output=output,
        picture=PictureHooks(transport=_openrouter(_picture, seen), brief_writer=FakeBriefs()),
    )

    assert code == 1
    assert not out.exists()
    assert "/api/v1/images" not in seen
    assert output.getvalue().startswith(
        "generate-exercise-picture: result=cap_reached reason=over_cap "
    )
    assert await _image_rows(admin_id) == []


async def test_a_name_in_more_than_one_sport_is_refused_before_any_call(
    admin_id: UUID, tmp_path: Path
) -> None:
    engine = build_engine(str(Settings().database_url))
    try:
        await SqlAlchemyExerciseRepository(build_session_factory(engine)).upsert_catalogue(
            [
                CatalogueExercise(
                    sport_id="climbing",
                    source="zz-pic-cli",
                    source_ref="zz-pic-cli-pull-up-climbing",
                    name=_NAME.upper(),
                    equipment="body weight",
                    body_part="back",
                    target="lats",
                    secondary_muscles=(),
                    default_measure=ExerciseMeasure.REPS,
                    attribution="test",
                    texts=(ExerciseText(lang="en", instructions="Hang, then pull.", steps=()),),
                )
            ]
        )
    finally:
        await engine.dispose()
    output = io.StringIO()
    seen: list[str] = []
    briefs = FakeBriefs()
    out = tmp_path / "pictures"

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--out", str(out)],
        output=output,
        picture=PictureHooks(transport=_openrouter(_picture, seen), brief_writer=briefs),
    )

    assert code == 1
    assert output.getvalue() == "generate-exercise-picture: result=refused reason=ambiguous_name\n"
    assert seen == []
    assert briefs.seen == []
    assert not out.exists()


async def _links(exercise_name: str) -> list[dict[str, object]]:
    engine = build_engine(str(Settings().database_url))
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT em.id AS link_id, em.status, em.position, a.id AS asset_id, "
                    "a.storage_key, a.sha256, a.embed_url, a.rights, a.delivery, a.source_ref "
                    "FROM exercise_media em JOIN exercises e ON e.id = em.exercise_id "
                    "JOIN media_assets a ON a.id = em.asset_id WHERE e.name = :name "
                    "ORDER BY em.position"
                ),
                {"name": exercise_name},
            )
            return [dict(row) for row in rows.mappings()]
    finally:
        await engine.dispose()


async def test_store_writes_a_suggested_link(admin_id: UUID) -> None:
    """AC (#535): `--store` uploads the picture and writes a licensed, stored asset with a
    `suggested` link; the card query doesn't return it."""
    output = io.StringIO()
    bucket = InMemoryBucket()

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--store"],
        output=output,
        picture=PictureHooks(
            transport=_openrouter(_picture, []), brief_writer=FakeBriefs(), storage=bucket
        ),
    )

    assert code == 0
    [link] = await _links(_NAME)
    assert link["status"] == "suggested"
    assert (link["rights"], link["delivery"], link["embed_url"]) == ("licensed", "stored", None)
    assert link["source_ref"] == Settings().image_generation_model
    assert bucket.objects == {str(link["storage_key"]): _PICTURE}
    assert output.getvalue().startswith(
        f"generate-exercise-picture: result=stored asset={link['asset_id']} exercise="
    )
    assert output.getvalue().endswith(" link=suggested spent_usd=0.0336004\n")


async def test_store_with_out_writes_the_files_too(admin_id: UUID, tmp_path: Path) -> None:
    out = tmp_path / "pictures"

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--store", "--out", str(out)],
        output=io.StringIO(),
        picture=PictureHooks(
            transport=_openrouter(_picture, []), brief_writer=FakeBriefs(), storage=InMemoryBucket()
        ),
    )

    assert code == 0
    assert (out / "zz-pic-cli-pull-up.jpg").read_bytes() == _PICTURE
    assert len(await _links(_NAME)) == 1


async def test_a_failed_file_write_writes_no_rows(admin_id: UUID, tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory")

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--store", "--out", str(blocked / "pictures")],
        output=io.StringIO(),
        picture=PictureHooks(
            transport=_openrouter(_picture, []), brief_writer=FakeBriefs(), storage=InMemoryBucket()
        ),
    )

    assert code == 1
    assert await _links(_NAME) == []


async def test_a_storage_failure_prints_the_reason_and_writes_no_row(admin_id: UUID) -> None:
    output = io.StringIO()

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--store"],
        output=output,
        picture=PictureHooks(
            transport=_openrouter(_picture, []),
            brief_writer=FakeBriefs(),
            storage=InMemoryBucket(fail_put=True),
        ),
    )

    assert code == 1
    assert output.getvalue() == (
        "generate-exercise-picture: result=failed reason=storage_error spent_usd=0.0336004\n"
    )
    assert await _links(_NAME) == []


async def test_store_without_a_media_bucket_is_refused_before_any_call(
    admin_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OBJECT_STORAGE_MEDIA_BUCKET", raising=False)
    monkeypatch.delenv("OBJECT_STORAGE_ENDPOINT", raising=False)
    output = io.StringIO()
    seen: list[str] = []
    briefs = FakeBriefs()

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME, "--store"],
        output=output,
        picture=PictureHooks(transport=_openrouter(_picture, seen), brief_writer=briefs),
    )

    assert code == 1
    assert output.getvalue() == (
        "generate-exercise-picture: result=refused reason=storage_not_configured\n"
    )
    assert seen == []
    assert briefs.seen == []


async def test_no_out_and_no_store_is_refused(admin_id: UUID) -> None:
    output = io.StringIO()
    seen: list[str] = []

    code = await cli.run(
        [GENERATE_EXERCISE_PICTURE, _NAME],
        output=output,
        picture=PictureHooks(transport=_openrouter(_picture, seen), brief_writer=FakeBriefs()),
    )

    assert code == 1
    assert output.getvalue() == "generate-exercise-picture: result=refused reason=no_destination\n"
    assert seen == []
