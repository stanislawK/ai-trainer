from uuid import uuid4

from ai_trainer.application.csrf import csrf_token_is_valid, generate_csrf_token

SECRET = b"test-secret"


def test_generate_csrf_token_is_deterministic_for_the_same_session_and_secret() -> None:
    session_id = uuid4()

    assert generate_csrf_token(session_id, SECRET) == generate_csrf_token(session_id, SECRET)


def test_generate_csrf_token_differs_across_sessions() -> None:
    assert generate_csrf_token(uuid4(), SECRET) != generate_csrf_token(uuid4(), SECRET)


def test_generate_csrf_token_differs_across_secrets() -> None:
    session_id = uuid4()

    assert generate_csrf_token(session_id, SECRET) != generate_csrf_token(session_id, b"other")


def test_csrf_token_is_valid_accepts_the_matching_token() -> None:
    session_id = uuid4()
    token = generate_csrf_token(session_id, SECRET)

    assert csrf_token_is_valid(token, session_id=session_id, secret_key=SECRET)


def test_csrf_token_is_valid_rejects_a_token_from_another_session() -> None:
    session_id = uuid4()
    foreign_token = generate_csrf_token(uuid4(), SECRET)

    assert not csrf_token_is_valid(foreign_token, session_id=session_id, secret_key=SECRET)


def test_csrf_token_is_valid_rejects_a_garbage_token() -> None:
    assert not csrf_token_is_valid("not-a-real-token", session_id=uuid4(), secret_key=SECRET)
