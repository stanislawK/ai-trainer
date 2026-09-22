from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT_USER = text("INSERT INTO users (id, sub, email) VALUES (:id, :sub, :email)")
_INSERT_SESSION = text(
    "INSERT INTO sessions (id, user_id, expires_at) "
    "VALUES (:id, :user_id, now() + interval '14 days')"
)
_DELETE_USER = text("DELETE FROM users WHERE id = :id")
_COUNT_SESSIONS_FOR_USER = text("SELECT count(*) FROM sessions WHERE user_id = :user_id")


async def test_deleting_a_user_cascades_their_sessions(db_session: AsyncSession) -> None:
    user_id = uuid4()
    session_id = uuid4()
    await db_session.execute(
        _INSERT_USER, {"id": user_id, "sub": str(user_id), "email": f"{user_id}@example.com"}
    )
    await db_session.execute(_INSERT_SESSION, {"id": session_id, "user_id": user_id})
    await db_session.commit()

    before = await db_session.execute(_COUNT_SESSIONS_FOR_USER, {"user_id": user_id})
    assert before.scalar_one() == 1

    await db_session.execute(_DELETE_USER, {"id": user_id})
    await db_session.commit()

    after = await db_session.execute(_COUNT_SESSIONS_FOR_USER, {"user_id": user_id})
    assert after.scalar_one() == 0
