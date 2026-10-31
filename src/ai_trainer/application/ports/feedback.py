from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.feedback import FeedbackEvent, FeedbackSignal, NewFeedbackEvent


class FeedbackEventsPort(Protocol):
    """Persists `feedback_events` (ADR-0034): content-free, user-owned rows. Only `record` and
    `list_for_user` are scoped to a user; `signals` is the owner's report over everyone's rows
    and returns neither user nor ID."""

    async def record(self, event: NewFeedbackEvent) -> FeedbackEvent: ...

    async def list_for_user(self, user_id: UUID) -> Sequence[FeedbackEvent]: ...

    async def signals(self, *, since: datetime | None = None) -> Sequence[FeedbackSignal]:
        """Every user's events at or after `since`, without who made them."""
        ...
