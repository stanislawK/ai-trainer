from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class NewSession(BaseModel):
    """A session about to be persisted; the repository assigns `id` and `created_at`."""

    user_id: UUID
    expires_at: datetime


class Session(NewSession):
    """A persisted `sessions` row (ADR-0005): the PostgreSQL-backed login session."""

    id: UUID
    created_at: datetime
