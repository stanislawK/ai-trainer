from uuid import uuid4

from ai_trainer.web.auth import parse_session_id


def test_parse_session_id_parses_a_valid_uuid() -> None:
    session_id = uuid4()

    assert parse_session_id(str(session_id)) == session_id


def test_parse_session_id_returns_none_for_a_malformed_value() -> None:
    assert parse_session_id("not-a-uuid") is None


def test_parse_session_id_returns_none_for_no_cookie() -> None:
    assert parse_session_id(None) is None
