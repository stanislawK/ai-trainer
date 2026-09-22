from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT_USER = text("INSERT INTO users (id) VALUES (:id)")
_INSERT_CALL = text(
    "INSERT INTO llm_calls "
    "(id, user_id, template_id, template_version, model, input_tokens, output_tokens, "
    "cost, latency_ms, outcome) "
    "VALUES "
    "(:id, :user_id, 'greeter', 1, 'openai/gpt-5-mini', 10, 5, 0.0012, 42, 'success')"
)
_DELETE_USER = text("DELETE FROM users WHERE id = :id")
_COUNT_CALLS_FOR_USER = text("SELECT count(*) FROM llm_calls WHERE user_id = :user_id")


async def test_deleting_a_user_cascades_their_llm_calls(db_session: AsyncSession) -> None:
    user_id = uuid4()
    call_id = uuid4()
    await db_session.execute(_INSERT_USER, {"id": user_id})
    await db_session.execute(_INSERT_CALL, {"id": call_id, "user_id": user_id})
    await db_session.commit()

    before = await db_session.execute(_COUNT_CALLS_FOR_USER, {"user_id": user_id})
    assert before.scalar_one() == 1

    await db_session.execute(_DELETE_USER, {"id": user_id})
    await db_session.commit()

    after = await db_session.execute(_COUNT_CALLS_FOR_USER, {"user_id": user_id})
    assert after.scalar_one() == 0
