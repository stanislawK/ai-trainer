"""Seeds a pending gym draft holding a circuit into a dev user's chat (ticket #632).

Run with `uv run python -m scripts.seed_circuit_draft [--sub <sub>]` against the database the app
reads (`DATABASE_URL`), after `scripts/dev_session.py` has seeded its user (the default `--sub`).
`gym.extract` doesn't build circuits until #633, so this is how the demo reaches one: it opens a
new conversation with the log "3 rounds: 10 burpees, 15 squats, then a 45 s plank", a reply, and
under it the draft the extraction would make, three rounds of burpees and squats in one circuit
and a plank outside it. The athlete edits, confirms or discards it in the chat like any draft.
It is a dev script, never a route, and makes no model call.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TextIO
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.sports.gym.payload import GYM_PAYLOAD_VERSION, GymPayload
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.main import build_activity_stores
from ai_trainer.settings import Settings
from scripts.dev_session import DEV_SESSION_SUB

LOG = "3 rounds: 10 burpees, 15 squats, then a 45 s plank"
REPLY = "Here's the session."
REPLY_AFTER = timedelta(seconds=1)
CIRCUIT = GymPayload.model_validate(
    {
        "activities": [
            {
                "name_as_typed": "burpees",
                "measure": "reps",
                "circuit": 1,
                "rounds": 3,
                "sets": [{"reps": 10}] * 3,
            },
            {
                "name_as_typed": "squats",
                "measure": "reps",
                "circuit": 1,
                "rounds": 3,
                "sets": [{"reps": 15}] * 3,
            },
            {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 45}]},
        ]
    }
)


async def seed_circuit_draft(
    factory: Callable[[], AsyncSession], clock: ClockPort, *, sub: str, retention_days: int
) -> StoredDraft:
    """The circuit draft, pending in a new conversation of the user `sub`, dated today in their
    timezone. Raises `LookupError` when no such user was seeded."""
    user = await SqlAlchemyUsersRepository(factory).get_by_sub(sub)
    if user is None:
        raise LookupError(f"no user {sub!r}: run scripts/dev_session.py first")
    now = clock.now()
    conversation = await SqlAlchemyConversationRepository(
        factory, clock=clock, retention_days=retention_days
    ).create(user.id, None, now)
    chat = SqlAlchemyChatRepository(factory)
    log = await chat.add(user.id, conversation.id, ChatRole.USER, LOG, now)
    # The reply comes after the log, never at the same instant: on a tie the chat can read the
    # log as unanswered and run a real extraction on it.
    replied = now + REPLY_AFTER
    await chat.add(
        user.id, conversation.id, ChatRole.ASSISTANT, REPLY, replied,
        sports=("gym",), template_id="gym.extract",
    )  # fmt: skip
    registry = default_sport_registry()
    drafts = SqlAlchemySessionDraftRepository(
        factory, registry, stores=build_activity_stores(registry)
    )
    return await drafts.add(
        user.id,
        conversation.id,
        log.id,
        "gym",
        SessionDraft(
            occurred_on=now.astimezone(ZoneInfo(user.timezone)).date(),
            date_source="assumed_today",
            timezone=user.timezone,
            payload=CIRCUIT.model_dump(mode="json"),
        ),
        payload_version=GYM_PAYLOAD_VERSION,
        created_at=replied,
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sub", default=DEV_SESSION_SUB, help="the seeded user's sub")
    return parser.parse_args(argv)


async def main(
    *, settings: Settings | None = None, output: TextIO = sys.stdout, sub: str = DEV_SESSION_SUB
) -> UUID:
    settings = settings if settings is not None else Settings()
    engine = build_engine(str(settings.database_url))
    try:
        draft = await seed_circuit_draft(
            build_session_factory(engine),
            UtcClock(),
            sub=sub,
            retention_days=settings.conversation_retention_days,
        )
    finally:
        await engine.dispose()
    output.write(f"circuit draft {draft.id} in conversation {draft.conversation_id}\n")
    return draft.id


if __name__ == "__main__":
    asyncio.run(main(sub=parse_args(sys.argv[1:]).sub))  # pragma: no cover -- direct run only
