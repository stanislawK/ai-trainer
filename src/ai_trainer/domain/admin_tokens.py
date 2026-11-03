"""Admin tokens, the credential the knowledge staging routes take (ADR-0036, #455).

A token is `atk_` followed by 32 random bytes in unpadded base64url. It is shown to its admin
once; only its SHA-256 and a display prefix are stored, and it expires within 30 days. An
`AdminToken` read back from storage carries no hash, so nothing that renders one can leak it.
"""

import hashlib
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

TOKEN_PREFIX = "atk_"
TOKEN_BYTES = 32
# `atk_` and 8 characters of the random part: enough to tell tokens apart, far too few to guess
# the rest.
DISPLAY_PREFIX_LENGTH = 12
MAX_TOKEN_DAYS = 30


class TokenState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class InvalidTokenLifetimeError(ValueError):
    """A lifetime outside 1 to `MAX_TOKEN_DAYS` days."""


class AdminToken(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    display_prefix: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    def state_at(self, now: datetime) -> TokenState:
        if self.revoked_at is not None:
            return TokenState.REVOKED
        if now >= self.expires_at:
            return TokenState.EXPIRED
        return TokenState.ACTIVE


class NewAdminToken(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    token_hash: str
    display_prefix: str
    created_at: datetime
    expires_at: datetime


def token_hash(token: str) -> str:
    """The lowercase hex SHA-256 of the token's UTF-8 text, the only form it is stored in."""
    return hashlib.sha256(token.encode()).hexdigest()
