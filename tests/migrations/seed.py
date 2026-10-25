"""Seed rows for the upgrade-with-data test (ADR-0024, ticket #218).

At least one row in every user-owned table, for two users. A new user-owned table gets its rows
here in the same change that adds it, or the test's coverage check fails. `seed()` inserts only
the columns a table has at that moment, so the same rows fit `main`'s head and the branch head;
a column added later is simply left to its default on the older schema.

Values are fixed, so a failure names the same row every run. Every column holds a different
value for each user (a nullable one is NULL for one of them), timestamps carry seconds and
microseconds, and text and JSON aren't in a normal form, so a backfill or normalization run
without a `WHERE` changes at least one row (`test_every_seeded_column_differs_between_users`).
"""

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

Row = Mapping[str, object]

USER_A = UUID("00000000-0000-4000-8000-0000000000a1")
USER_B = UUID("00000000-0000-4000-8000-0000000000b1")
USERS = (USER_A, USER_B)


class SeedError(Exception):
    """A seed row does not fit the schema it is inserted into."""


def _id(user: UUID, table: int) -> UUID:
    """A fixed id per user and table, e.g. `…00a1-0000000000000007` for user A's table 7."""
    return UUID(f"{str(user)[:-12]}{str(user)[-2:]}{table:010d}")


def _at(n: int, hour: int) -> datetime:
    return datetime(2026, 10, n, hour, 10 + n, 20 + n, 123456 * n, tzinfo=UTC)


def _rows() -> dict[str, tuple[Row, ...]]:
    rows: dict[str, list[Row]] = {}
    for n, user in enumerate(USERS, start=1):
        a = n == 1  # user A's value first, user B's second in every `x if a else y`
        conversation, message, training, draft = (_id(user, i) for i in (2, 3, 4, 5))
        rows.setdefault("users", []).append(
            {
                "id": user,
                "created_at": _at(n, 6),
                "sub": f"seed-sub-{n}",
                "email": "seed-1@example.com" if a else "Seed-2@Example.COM",
                "name": "Seed Athlete 1" if a else "  Seed Athlete 2 ",
                "locale": "pl" if a else "de",
                "status": "active" if a else "pending",
                "onboarded_at": _at(n, 7) if a else None,
                "timezone": "Europe/Warsaw" if a else "Europe/Berlin",
            }
        )
        rows.setdefault("sessions", []).append(
            {"id": _id(user, 1), "user_id": user, "expires_at": _at(n, 23), "created_at": _at(n, 7)}
        )
        rows.setdefault("conversations", []).append(
            {
                "id": conversation,
                "user_id": user,
                "title": "Bouldering at Murall" if a else None,
                "created_at": _at(n, 8),
                "last_message_at": _at(n, 9),
            }
        )
        rows.setdefault("chat_messages", []).append(
            {
                "id": message,
                "user_id": user,
                "conversation_id": conversation,
                "role": "user" if a else "assistant",
                "text": "Climbed 6b+ and a 7a flash" if a else "  Logged: 40 km, 1:30 h ",
                "sports": ["climbing"] if a else ["gym", "cycling"],
                "template_id": None if a else "chitchat",
                "template_version": None if a else 2,
                "created_at": _at(n, 9),
            }
        )
        rows.setdefault("training_sessions", []).append(
            {
                "id": training,
                "user_id": user,
                "sport_id": "climbing" if a else "cycling",
                "occurred_on": date(2026, 10, n),
                "timezone": "Europe/Warsaw" if a else "Europe/Berlin",
                "started_at": _at(n, 10) if a else None,
                "duration_min": 91 if a else None,
                "rpe": 7 if a else None,
                "feeling": "strong" if a else None,
                "notes": "seed notes" if a else "  Seed Notes, 2 ",
                "payload": Jsonb(
                    {"routes": [{"grade": "6b+", "scale": "french"}], "legacy": None}
                    if a
                    else {"distance_km": 40.5, "elevation_m": None}
                ),
                "payload_version": 1 if a else 2,
                "created_at": _at(n, 11),
                "updated_at": _at(n, 12),
            }
        )
        rows.setdefault("training_session_sources", []).append(
            {
                "id": _id(user, 6),
                "user_id": user,
                "session_id": training,
                "raw_text": "Climbed 6b+ and a 7a flash" if a else "40 km ride",
                "source": "chat" if a else "manual",
                "logged_at": _at(n, 11),
            }
        )
        rows.setdefault("session_drafts", []).append(
            {
                "id": draft,
                "user_id": user,
                "conversation_id": conversation,
                "message_id": message,
                "sport_id": "climbing" if a else "cycling",
                "draft": Jsonb({"duration_min": 91, "rpe": None} if a else {"distance_km": 40.5}),
                "payload_version": 1 if a else 2,
                "extends_session_id": None if a else training,
                "status": "confirmed" if a else "pending",
                "session_id": training if a else None,
                "created_at": _at(n, 10),
                "resolved_at": _at(n, 11) if a else None,
            }
        )
        rows.setdefault("clarifications", []).append(
            {
                "id": _id(user, 7),
                "user_id": user,
                "conversation_id": conversation,
                "message_id": message,
                "draft_id": draft if a else None,
                "purpose": "sport" if a else "same_session",
                "clarification": Jsonb(
                    {"options": [{"id": "climbing"}, {"id": "gym"}], "hint": None}
                    if a
                    else {"options": [{"id": "same"}, {"id": "new"}]}
                ),
                "status": "answered" if a else "open",
                "answer_option_ids": ["climbing"] if a else None,
                "created_at": _at(n, 10),
                "answered_at": _at(n, 11) if a else None,
            }
        )
        rows.setdefault("llm_calls", []).append(
            {
                "id": _id(user, 8),
                "user_id": user,
                "template_id": "router" if a else "chitchat",
                "template_version": n,
                "model": "seed/model-a" if a else "seed/model-b",
                "input_tokens": 100 * n,
                "output_tokens": 10 * n,
                "cost": Decimal("0.000123") if a else None,
                "latency_ms": 400 + n,
                "outcome": "ok" if a else "error",
                "created_at": _at(n, 9),
            }
        )
        rows.setdefault("goals", []).append(
            {
                "id": _id(user, 9),
                "user_id": user,
                "position": n,
                "text": "Send 7a by spring" if a else "  Ride 100 km ",
                "target_date": date(2027, 4, 1) if a else None,
                "created_at": _at(n, 7),
                "sport_id": "climbing" if a else None,
            }
        )
        rows.setdefault("user_sports", []).append(
            {"user_id": user, "sport_id": "climbing" if a else "gym", "created_at": _at(n, 7)}
        )
        rows.setdefault("weekly_availability", []).append(
            {"user_id": user, "weekday": n, "minutes": 60 * n, "created_at": _at(n, 7)}
        )
        rows.setdefault("athlete_profiles", []).append(
            {
                "user_id": user,
                "birth_year": 1991 if a else None,
                "height_cm": 171 if a else None,
                "weight_kg": Decimal("61.5") if a else None,
                "limits": "left shoulder" if a else None,
            }
        )
        rows.setdefault("sport_profiles", []).append(
            {
                "user_id": user,
                "sport_id": "climbing" if a else "gym",
                "level": "Projecting 7b" if a else None,
                "exercises": " Board, hangboard " if a else None,
                "sessions_per_week": 4 if a else None,
                "session_minutes": 120 if a else None,
                "preferred_days": [5, 0, 2] if a else None,
            }
        )
        rows.setdefault("onboarding_answers", []).append(
            {
                "id": _id(user, 10),
                "user_id": user,
                "step": "about_you" if a else "goals",
                "sport_id": None if a else "climbing",
                "text": "I'm 35, 171 cm, 61,5 kg" if a else None,
                "revision": n,
                "status": "parsed" if a else "pending",
                "parsed": Jsonb({"height_cm": 171, "limits": None}) if a else None,
                "updated_at": _at(n, 8),
            }
        )
        # Each user once on either side of a status change.
        rows.setdefault("user_status_changes", []).append(
            {
                "id": _id(user, 11),
                "actor_user_id": user,
                "target_user_id": USER_B if a else USER_A,
                "old_status": "pending" if a else "active",
                "new_status": "active" if a else "disabled",
                "created_at": _at(n, 13),
            }
        )
    return {table: tuple(table_rows) for table, table_rows in rows.items()}


# Insertion order is foreign-key order: a row's parents come first.
SEED: Mapping[str, tuple[Row, ...]] = _rows()


def columns(conn: psycopg.Connection[Any], table: str) -> set[str]:
    cursor = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {row[0] for row in cursor}


def seed(conn: psycopg.Connection[Any], tables: Iterable[str]) -> None:
    """Insert the seed rows of `tables` that exist, in `SEED` order, with the columns they have."""
    wanted = set(tables)
    for table, table_rows in SEED.items():
        if table not in wanted:
            continue
        present = columns(conn, table)
        for number, row in enumerate(table_rows):
            fitting = [column for column in row if column in present]
            try:
                with conn.transaction():
                    conn.execute(
                        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                            sql.Identifier(table),
                            sql.SQL(", ").join(map(sql.Identifier, fitting)),
                            sql.SQL(", ").join(sql.Placeholder() * len(fitting)),
                        ),
                        [row[column] for column in fitting],
                    )
            except psycopg.Error as exc:
                raise SeedError(f"{table}: seed row {number} does not insert ({exc})") from exc
