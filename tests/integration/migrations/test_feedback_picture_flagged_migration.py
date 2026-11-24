"""Migrations `d4a8e2b6f1c9` and `e5b9f3c7a2d0` (ADR-0031 *Picture on the card*, ADR-0034,
ADR-0024, #644): `feedback_events`' kind check widened to admit `picture_flagged`, expand only.
Existing rows keep every value, the check ends validated, and a kind outside the set is still
refused."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_BEFORE = "9a6c3f1e5b27"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def test_picture_flagged_kind_upgrades_with_data() -> None:
    cfg = Config("alembic.ini")
    user_id, event_id, howto_id = uuid4(), uuid4(), uuid4()
    try:
        command.downgrade(cfg, _BEFORE)
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
                (user_id, str(user_id), f"{user_id}@example.com"),
            )
            cur.execute(
                "INSERT INTO feedback_events (id, user_id, kind, template_id, template_version, "
                "fields) VALUES (%s, %s, 'draft_edited', 'gym.extract', 3, '{reps}')",
                (event_id, user_id),
            )
            cur.execute(
                "INSERT INTO feedback_events (id, user_id, kind, template_id, template_version) "
                "VALUES (%s, %s, 'howto_flagged', 'exercise.simple_howto', 1)",
                (howto_id, user_id),
            )
            conn.commit()
            # Before the widening the kind is refused.
            with pytest.raises(psycopg.errors.CheckViolation, match="feedback_events_kind"):
                cur.execute(
                    "INSERT INTO feedback_events (id, user_id, kind) "
                    "VALUES (gen_random_uuid(), %s, 'picture_flagged')",
                    (user_id,),
                )
            conn.rollback()

        command.upgrade(cfg, "head")

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, template_id, template_version, fields FROM feedback_events "
                "WHERE id = %s",
                (event_id,),
            )
            assert cur.fetchone() == ("draft_edited", "gym.extract", 3, ["reps"])
            cur.execute(
                "SELECT kind, template_id, template_version FROM feedback_events WHERE id = %s",
                (howto_id,),
            )
            assert cur.fetchone() == ("howto_flagged", "exercise.simple_howto", 1)
            cur.execute(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'feedback_events_kind'"
            )
            assert cur.fetchone() == (True,)
            cur.execute(
                "INSERT INTO feedback_events (id, user_id, kind) "
                "VALUES (gen_random_uuid(), %s, 'picture_flagged')",
                (user_id,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="feedback_events_kind"):
                cur.execute(
                    "INSERT INTO feedback_events (id, user_id, kind) "
                    "VALUES (gen_random_uuid(), %s, 'something_else')",
                    (user_id,),
                )
            conn.rollback()
    finally:
        command.upgrade(cfg, "head")
