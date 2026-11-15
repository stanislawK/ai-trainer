"""Migrations `d5e2a8c41f07` and `e7a3c95b2d18` (ADR-0031 *Media links*, ADR-0024, #556):
`exercise_media.removed_at` and `removed_from`, expand only. Existing links keep every value with
both columns NULL, and `removed_from` holds only a status Restore can put back."""

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_BEFORE = "8f52610a9d41"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _link(cur: psycopg.Cursor[Any], status: str) -> UUID:
    exercise_id, asset_id, link_id = uuid4(), uuid4(), uuid4()
    cur.execute(
        "INSERT INTO exercises (id, sport_id, source, source_ref, name, default_measure) "
        "VALUES (%s, 'gym', 'dataset', %s, 'Row', 'reps')",
        (exercise_id, f"zz-removal-{exercise_id}"),
    )
    cur.execute(
        "INSERT INTO media_assets (id, kind, rights, delivery, embed_url, source, source_ref) "
        "VALUES (%s, 'video', 'unstated', 'embedded', %s, 'youtube', 'abc')",
        (asset_id, f"https://www.youtube.com/embed/{asset_id}"),
    )
    cur.execute(
        "INSERT INTO exercise_media (id, exercise_id, asset_id, position, status) "
        "VALUES (%s, %s, %s, 0, %s)",
        (link_id, exercise_id, asset_id, status),
    )
    return link_id


def test_removal_columns_upgrade_with_data() -> None:
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _BEFORE)
        with _connect() as conn, conn.cursor() as cur:
            links = {status: _link(cur, status) for status in ("approved", "removed")}
            conn.commit()

        command.upgrade(cfg, "head")

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, removed_at, removed_from FROM exercise_media "
                "WHERE id = ANY(%s)",
                (list(links.values()),),
            )
            assert {row[0]: row[1:] for row in cur.fetchall()} == {
                links["approved"]: ("approved", None, None),
                links["removed"]: ("removed", None, None),
            }
            cur.execute(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'exercise_media_removed_from'"
            )
            assert cur.fetchone() == (True,)
            with pytest.raises(psycopg.errors.CheckViolation, match="exercise_media_removed_from"):
                cur.execute(
                    "UPDATE exercise_media SET removed_from = 'removed' WHERE id = %s",
                    (links["removed"],),
                )
            conn.rollback()
    finally:
        command.upgrade(cfg, "head")
