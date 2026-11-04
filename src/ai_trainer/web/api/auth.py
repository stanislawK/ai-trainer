"""Bearer authentication on the knowledge API (ADR-0036 invariants 2 and 8, ADR-0005 as amended).

The token comes only from the `Authorization` header. A token anywhere in the query string is
refused before anything else runs, the endpoint's own query validation included, so a 422 never
echoes it. Every failure of the token itself is one 401, so no answer says which check failed.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ai_trainer.adapters.log_records import bind_user_id
from ai_trainer.application.admin_tokens import (
    TokenAuthenticated,
    TokenAuthenticator,
    TokenNotAdmin,
    TokenRejected,
)
from ai_trainer.domain.admin_tokens import TOKEN_PREFIX

# `auto_error=False`: a missing or non-bearer header comes back as None, and the 401 is ours.
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AdminToken",
    description="An admin token from /admin/tokens, as `Authorization: Bearer atk_…`.",
)
_QUERY_TOKEN_NAMES = frozenset({"access_token", "token"})


def refuse_token_in_query(request: Request) -> None:
    """400 for a token in the URI (research finding 3: it MUST NOT be in the query string),
    even beside a valid header."""
    for key, value in request.query_params.multi_items():
        if key.casefold() in _QUERY_TOKEN_NAMES or TOKEN_PREFIX in key or TOKEN_PREFIX in value:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Send the token in the Authorization header only.",
            )


def _not_authenticated() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def token_user(authenticator: TokenAuthenticator) -> Callable[..., Awaitable[UUID]]:
    """The dependency that yields the token's user, the only `user_id` on these routes."""

    async def authenticated(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> UUID:
        if credentials is None:
            raise _not_authenticated()
        outcome = await authenticator.authenticate(credentials.credentials)
        match outcome:
            case TokenAuthenticated(user_id=user_id):
                bind_user_id(user_id)
                return user_id
            case TokenRejected():
                raise _not_authenticated()
            case TokenNotAdmin():
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, detail="This token's account can't use the API."
                )

    return authenticated
