"""Migration `8f52610a9d41` (ADR-0031 "Media links", ADR-0024, ticket #530): `media_assets` and
`exercise_media`, created expand-only with ADR-0031's CHECKs. Upgrading with data in every table
is `tests/migrations/`' job (#218)."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_REVISION = "8f52610a9d41"
_PREVIOUS_REVISION = "a4c19e7b5d20"
_NEW_TABLES = {"media_assets", "exercise_media"}
_SHA = "b" * 64
_COLUMNS = (
    "kind",
    "rights",
    "delivery",
    "storage_key",
    "embed_url",
    "source",
    "source_ref",
    "license",
    "attribution",
    "sha256",
    "width",
    "height",
)


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _tables() -> set[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


@pytest.fixture
def cur() -> Iterator[psycopg.Cursor[Any]]:
    """A cursor whose transaction is rolled back, so nothing a test inserts is kept."""
    with _connect() as conn, conn.cursor() as cursor:
        try:
            yield cursor
        finally:
            conn.rollback()


def _stored(**overrides: object) -> dict[str, object]:
    return {
        "kind": "image",
        "rights": "licensed",
        "delivery": "stored",
        "storage_key": f"wger/{uuid4()}.png",
        "embed_url": None,
        "source": "wger",
        "source_ref": "https://wger.de/api/v2/exerciseimage/1/",
        "license": "CC BY-SA 4.0",
        "attribution": "Author · CC BY-SA 4.0 — via wger.de",
        "sha256": uuid4().hex * 2,
        "width": 800,
        "height": 600,
    } | overrides


def _embedded(**overrides: object) -> dict[str, object]:
    return {
        "kind": "video",
        "rights": "unstated",
        "delivery": "embedded",
        "storage_key": None,
        "embed_url": f"https://www.youtube.com/embed/{uuid4()}",
        "source": "youtube",
        "source_ref": "abc",
        "license": None,
        "attribution": None,
        "sha256": None,
        "width": None,
        "height": None,
    } | overrides


def _insert_asset(cur: psycopg.Cursor[Any], row: dict[str, object]) -> UUID:
    asset_id = uuid4()
    placeholders = ", ".join(["%s"] * (len(_COLUMNS) + 1))
    cur.execute(
        f"INSERT INTO media_assets (id, {', '.join(_COLUMNS)}) VALUES ({placeholders})",
        (asset_id, *(row[column] for column in _COLUMNS)),
    )
    return asset_id


def _insert_exercise(cur: psycopg.Cursor[Any]) -> UUID:
    exercise_id = uuid4()
    cur.execute(
        "INSERT INTO exercises (id, sport_id, source, source_ref, name, default_measure) "
        "VALUES (%s, 'gym', 'dataset', %s, 'Row', 'reps')",
        (exercise_id, f"zz-media-{exercise_id}"),
    )
    return exercise_id


def _link(
    cur: psycopg.Cursor[Any], exercise_id: UUID, asset_id: UUID, status: str = "approved"
) -> None:
    cur.execute(
        "INSERT INTO exercise_media (id, exercise_id, asset_id, position, status) "
        "VALUES (%s, %s, %s, 0, %s)",
        (uuid4(), exercise_id, asset_id, status),
    )


def test_media_tables_upgrade_and_downgrade() -> None:
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _REVISION)
        at_revision = _tables()
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert at_revision - _tables() == _NEW_TABLES
        command.upgrade(cfg, _REVISION)
        assert _tables() == at_revision
    finally:
        command.upgrade(cfg, "head")


def test_media_check_rejects_stored_without_sha256(cur: psycopg.Cursor[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="media_assets_stored"):
        _insert_asset(cur, _stored(sha256=None))


def test_media_check_rejects_licensed_without_attribution(cur: psycopg.Cursor[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="media_assets_licensed"):
        _insert_asset(cur, _stored(attribution=None))


@pytest.mark.parametrize(
    ("row", "constraint"),
    [
        (_stored(storage_key=None), "media_assets_stored"),
        (_stored(embed_url="https://example.com/x.png"), "media_assets_stored"),
        (_embedded(embed_url=None), "media_assets_embedded"),
        (_embedded(storage_key="wger/x.png"), "media_assets_embedded"),
        (_stored(license=None), "media_assets_licensed"),
        (
            _stored(rights="unstated", license=None, attribution=None),
            "media_assets_unstated",
        ),
        (_stored(kind="audio"), "media_assets_kind"),
        (_stored(rights="public"), "media_assets_rights"),
        (_stored(delivery="hotlinked"), "media_assets_delivery"),
        (_stored(sha256="ABC"), "media_assets_sha256_format"),
        (_stored(width=0), "media_assets_size"),
        (_stored(source=""), "media_assets_source_present"),
        (_stored(attribution="  "), "media_assets_licensed"),
        (_stored(license=""), "media_assets_licensed"),
        (_embedded(embed_url="javascript:alert(1)"), "media_assets_embed_url_scheme"),
        (_stored(width=100, height=None), "media_assets_size"),
    ],
    ids=[
        "stored-no-key",
        "stored-with-embed",
        "embedded-no-url",
        "embedded-with-key",
        "licensed-no-license",
        "unstated-stored",
        "bad-kind",
        "bad-rights",
        "bad-delivery",
        "bad-sha256",
        "zero-width",
        "blank-source",
        "blank-attribution",
        "blank-license",
        "embed-not-http",
        "half-set-size",
    ],
)
def test_media_checks_reject_invalid_rows(
    cur: psycopg.Cursor[Any], row: dict[str, object], constraint: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        _insert_asset(cur, row)


def test_media_checks_accept_valid_rows(cur: psycopg.Cursor[Any]) -> None:
    _insert_asset(cur, _stored())
    _insert_asset(cur, _embedded())
    _insert_asset(
        cur, _embedded(rights="licensed", license="CC BY 4.0", attribution="Owner · CC BY 4.0")
    )


def test_media_link_status_is_a_closed_set(cur: psycopg.Cursor[Any]) -> None:
    exercise_id = _insert_exercise(cur)
    asset_id = _insert_asset(cur, _stored())

    with pytest.raises(psycopg.errors.CheckViolation, match="exercise_media_status"):
        _link(cur, exercise_id, asset_id, status="hidden")


def test_media_link_is_unique_per_exercise_and_asset(cur: psycopg.Cursor[Any]) -> None:
    exercise_id = _insert_exercise(cur)
    asset_id = _insert_asset(cur, _stored())
    _link(cur, exercise_id, asset_id)
    _link(cur, _insert_exercise(cur), asset_id)

    with pytest.raises(psycopg.errors.UniqueViolation, match="exercise_media_exercise_asset_key"):
        _link(cur, exercise_id, asset_id, status="suggested")


def test_media_sha256_and_embed_url_are_unique_where_set(cur: psycopg.Cursor[Any]) -> None:
    stored = _stored()
    embedded = _embedded()
    _insert_asset(cur, stored)
    _insert_asset(cur, embedded)
    # NULLs don't collide: a second embedded row has no sha256, a second stored one no URL.
    _insert_asset(cur, _embedded())
    _insert_asset(cur, _stored())

    cur.execute("SAVEPOINT duplicate")
    with pytest.raises(psycopg.errors.UniqueViolation, match="media_assets_sha256_key"):
        _insert_asset(cur, _stored(sha256=stored["sha256"]))
    cur.execute("ROLLBACK TO SAVEPOINT duplicate")
    with pytest.raises(psycopg.errors.UniqueViolation, match="media_assets_embed_url_key"):
        _insert_asset(cur, _embedded(embed_url=embedded["embed_url"]))


def test_deleting_an_exercise_drops_its_links_and_keeps_the_asset(
    cur: psycopg.Cursor[Any],
) -> None:
    exercise_id = _insert_exercise(cur)
    asset_id = _insert_asset(cur, _stored())
    _link(cur, exercise_id, asset_id)

    cur.execute("DELETE FROM exercises WHERE id = %s", (exercise_id,))

    cur.execute("SELECT count(*) FROM exercise_media WHERE asset_id = %s", (asset_id,))
    assert cur.fetchone() == (0,)
    cur.execute("SELECT count(*) FROM media_assets WHERE id = %s", (asset_id,))
    assert cur.fetchone() == (1,)


def test_a_linked_asset_cannot_be_deleted(cur: psycopg.Cursor[Any]) -> None:
    asset_id = _insert_asset(cur, _stored())
    _link(cur, _insert_exercise(cur), asset_id)

    with pytest.raises(psycopg.errors.RestrictViolation):
        cur.execute("DELETE FROM media_assets WHERE id = %s", (asset_id,))
