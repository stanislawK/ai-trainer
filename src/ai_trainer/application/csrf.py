"""CSRF protection (ADR-0005 invariant 3): a stateless, signed token bound to the session.

The token is the HMAC-SHA256 of the session id under a server secret, so a submitted token is
valid only for the session it was generated for -- forging one for a different session, or
without the secret, is infeasible. Nothing is stored server-side; the server just recomputes
the expected token from the session id and compares.
"""

import hmac
from hashlib import sha256
from uuid import UUID

_MESSAGE_PREFIX = b"csrf:"


def generate_csrf_token(session_id: UUID, secret_key: bytes) -> str:
    return hmac.new(secret_key, _MESSAGE_PREFIX + str(session_id).encode(), sha256).hexdigest()


def csrf_token_is_valid(token: str, *, session_id: UUID, secret_key: bytes) -> bool:
    expected = generate_csrf_token(session_id, secret_key)
    return hmac.compare_digest(expected, token)
