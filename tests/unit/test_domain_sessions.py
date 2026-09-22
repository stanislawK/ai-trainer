from datetime import UTC, datetime
from uuid import uuid4

from ai_trainer.domain.sessions import NewSession, Session


def test_session_extends_new_session_with_persisted_fields() -> None:
    new = NewSession(user_id=uuid4(), expires_at=datetime(2026, 10, 1, tzinfo=UTC))

    session = Session(
        id=uuid4(),
        user_id=new.user_id,
        expires_at=new.expires_at,
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
    )

    assert session.user_id == new.user_id
    assert session.expires_at == new.expires_at
