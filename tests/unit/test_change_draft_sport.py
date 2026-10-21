"""Changing a draft's sport on its card (PRD-0005 B23, F2, ticket #163): the switch re-runs the
chosen sport's extraction on the same log and replaces the pending draft in place. Only the
athlete's own sports that can be logged are offered, the switch writes no session, and a failed
extraction leaves the draft as it was."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.reply import ReplyFailed, change_draft_sport, sports_to_switch_to
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.sports.cycling.payload import CYCLING_PAYLOAD_VERSION
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
    DraftSportNotOfferedError,
    SessionDraft,
    SessionSpan,
    StoredDraft,
)
from tests.chat_support import (
    FROZEN_NOW,
    UNLOGGABLE_SPORT,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    InMemoryTrainingSessions,
    ScriptedConversation,
    reply_services,
    sports_with_rowing,
)

_INTERVALS = "Did intervals 45 min"
_TODAY = date(2026, 10, 2)
_GYM_INTERVALS = GymExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "assumed_today",
        "activities": [{"name": "Intervals"}],
    }
)
_RIDE_INTERVALS = CyclingExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "assumed_today",
        "activities": [{"moving_time_min": 45, "zones": [{"zone": 4}]}],
    }
)


class _Setup:
    def __init__(self, *, sports: tuple[str, ...] = ("gym", "cycling")) -> None:
        self.chat = InMemoryChatRepository()
        self.drafts = InMemoryDraftRepository()
        self.sessions = InMemoryTrainingSessions()
        self.user_id = uuid4()
        self.profile = InMemoryProfile({self.user_id: list(sports)})
        self.conversation = ScriptedConversation(extractions=[_RIDE_INTERVALS])

    @property
    def services(self):  # type: ignore[no-untyped-def]
        return reply_services(
            self.chat,
            drafts=self.drafts,
            sessions=self.sessions,
            profile=self.profile,
            conversation=self.conversation,
        )

    async def gym_draft(self) -> StoredDraft:
        """The athlete's "Did intervals 45 min", its gym extraction reply and pending draft."""
        conversation_id = await self.chat.conversation_of(self.user_id)
        log = await self.chat.add(
            self.user_id, conversation_id, ChatRole.USER, _INTERVALS, FROZEN_NOW
        )
        await self.chat.add(
            self.user_id,
            conversation_id,
            ChatRole.ASSISTANT,
            "Here's the session.",
            FROZEN_NOW + timedelta(microseconds=1),
            sports=("gym",),
            template_id="gym.extract",
            template_version=1,
        )
        return await self.drafts.add(
            self.user_id,
            conversation_id,
            log.id,
            "gym",
            SessionDraft(
                occurred_on=_TODAY,
                date_source="assumed_today",
                timezone="Europe/Warsaw",
                payload=_GYM_INTERVALS.to_payload().model_dump(mode="json"),
            ),
            payload_version=1,
            created_at=FROZEN_NOW,
        )

    async def switch(self, draft_id: UUID, sport_id: str) -> StoredDraft | ReplyFailed:
        return await change_draft_sport(
            self.user_id, draft_id, sport_id, timezone="Europe/Warsaw", services=self.services
        )


async def test_switching_a_gym_draft_to_cycling_extracts_the_same_log_as_a_ride() -> None:
    setup = _Setup()
    draft = await setup.gym_draft()

    switched = await setup.switch(draft.id, "cycling")

    assert isinstance(switched, StoredDraft)
    ((context, sport_id),) = setup.conversation.extracted
    assert (context.message, sport_id) == (_INTERVALS, "cycling")
    assert (switched.id, switched.status, switched.sport_id) == (draft.id, "pending", "cycling")
    assert switched.message_id == draft.message_id
    assert switched.payload_version == CYCLING_PAYLOAD_VERSION
    assert switched.draft.payload == _RIDE_INTERVALS.to_payload().model_dump(mode="json")
    assert setup.drafts.all_of(setup.user_id) == [switched]


async def test_the_switched_draft_extends_the_new_sports_session_when_close_after_it() -> None:
    """B16 is checked again for the new sport: the ride came 10 minutes after a ride."""
    setup = _Setup()
    draft = await setup.gym_draft()
    span = SessionSpan(
        id=uuid4(),
        occurred_on=_TODAY,
        first_logged_at=FROZEN_NOW - timedelta(minutes=40),
        last_logged_at=FROZEN_NOW - timedelta(minutes=10),
    )
    setup.sessions.latest[(setup.user_id, "cycling")] = span
    setup.drafts.spans[span.id] = span

    switched = await setup.switch(draft.id, "cycling")

    assert isinstance(switched, StoredDraft)
    assert setup.drafts.extends_ids[draft.id] == span.id
    assert switched.extends == span


async def test_switching_writes_no_session() -> None:
    setup = _Setup()
    draft = await setup.gym_draft()

    await setup.switch(draft.id, "cycling")

    assert setup.drafts.confirmed_at == {}
    assert [stored.session_id for stored in setup.drafts.all_of(setup.user_id)] == [None]


async def test_only_the_athletes_other_loggable_sports_are_offered() -> None:
    """In the athlete's own order, without the draft's sport or one with no extraction."""
    registry = sports_with_rowing()

    offered = sports_to_switch_to(["cycling", UNLOGGABLE_SPORT, "gym", "climbing"], "gym", registry)

    assert offered == ("cycling", "climbing")
    assert sports_to_switch_to(["gym"], "gym", registry) == ()
    assert sports_to_switch_to(["gym", "unknown"], "gym", registry) == ()


@pytest.mark.parametrize("sport_id", ["climbing", "gym", UNLOGGABLE_SPORT, "unknown"])
async def test_a_sport_not_offered_is_refused_with_no_call_and_no_change(sport_id: str) -> None:
    """Climbing isn't the athlete's, gym is the draft's own, rowing can't be logged."""
    setup = _Setup(sports=("gym", "cycling", UNLOGGABLE_SPORT))
    draft = await setup.gym_draft()

    with pytest.raises(DraftSportNotOfferedError):
        await setup.switch(draft.id, sport_id)

    assert setup.conversation.extracted == []
    assert setup.drafts.all_of(setup.user_id) == [draft]


@pytest.mark.parametrize("outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR])
async def test_a_failed_extraction_keeps_the_previous_draft(outcome: LlmCallOutcome) -> None:
    setup = _Setup()
    setup.conversation.extract_outcomes = [outcome]
    draft = await setup.gym_draft()

    failed = await setup.switch(draft.id, "cycling")

    assert failed == ReplyFailed(outcome=outcome)
    assert setup.drafts.all_of(setup.user_id) == [draft]


@pytest.mark.parametrize(
    ("status", "error"),
    [("confirmed", DraftConfirmedError), ("discarded", DraftDiscardedError)],
)
async def test_a_resolved_draft_is_refused_with_no_call(
    status: str, error: type[Exception]
) -> None:
    setup = _Setup()
    draft = await setup.gym_draft()
    setup.drafts.set_status(draft.id, status)  # type: ignore[arg-type]

    with pytest.raises(error):
        await setup.switch(draft.id, "cycling")

    assert setup.conversation.extracted == []


async def test_another_users_draft_is_not_found_and_stays() -> None:
    setup = _Setup()
    draft = await setup.gym_draft()
    owner = setup.user_id
    setup.user_id = uuid4()
    setup.profile.sports[setup.user_id] = ["gym", "cycling"]

    with pytest.raises(DraftNotFoundError):
        await setup.switch(draft.id, "cycling")

    assert setup.conversation.extracted == []
    assert setup.drafts.all_of(owner) == [draft]


async def test_the_switch_reads_the_log_as_of_when_it_was_sent() -> None:
    """A log sent before midnight and switched after it still means the day it was sent
    (ADR-0014): the extraction's "now" is the log's own time, not the switch's."""
    setup = _Setup()
    draft = await setup.gym_draft()
    services = replace(setup.services, clock=_Later())

    await change_draft_sport(
        setup.user_id, draft.id, "cycling", timezone="Europe/Warsaw", services=services
    )

    ((context, _),) = setup.conversation.extracted
    assert context.now == FROZEN_NOW


class _Later:
    def now(self) -> datetime:
        return FROZEN_NOW + timedelta(hours=15)
