"""A fake `FeedbackEventsPort` for unit tests (ADR-0034)."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from ai_trainer.domain.feedback import FeedbackEvent, FeedbackSignal, NewFeedbackEvent


class InMemoryFeedbackEvents:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[FeedbackEvent] = []
        self._fail = fail

    async def record(self, event: NewFeedbackEvent) -> FeedbackEvent:
        if self._fail:
            raise RuntimeError("feedback store is down")
        stored = FeedbackEvent(id=uuid4(), **event.model_dump())
        self.events.append(stored)
        return stored

    async def list_for_user(self, user_id: UUID) -> Sequence[FeedbackEvent]:
        return [event for event in self.events if event.user_id == user_id]

    async def signals(self, *, since: datetime | None = None) -> Sequence[FeedbackSignal]:
        return [
            FeedbackSignal(
                kind=event.kind,
                template_id=event.template_id,
                template_version=event.template_version,
                fields=event.fields,
            )
            for event in self.events
            if since is None or event.created_at >= since
        ]
