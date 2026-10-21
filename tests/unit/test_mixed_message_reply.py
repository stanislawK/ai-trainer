"""A message that logs several sports (PRD-0005 B1, B14, F15, ticket #161): each sport gets its
own extraction, run at once, and its own pending draft, in message order, in one reply. One
extraction that fails no longer sinks the others: its part is saved as a "couldn't draft this
one" line in its sport, and Retry runs that sport's extraction again and turns the line into
the draft's intro in place (ADR-0008 amendment at #161)."""

import asyncio
from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.reply import (
    PART_BREAK,
    FailedPartNotFoundError,
    MessageNotFoundError,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    RetriedDraft,
    RetryFailed,
    retry_draft,
    start_reply,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.training import DRAFT_FAILED_TEMPLATE_ID, StoredDraft
from tests.chat_support import (
    DRAFT_FAILED_TEXT,
    DRAFT_READY_TEXT,
    FROZEN_NOW,
    THREE_6A_TWO_6B,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    ScriptedConversation,
    reply_services,
)

_MIXED = "Bouldered 4 6B then rode home 15 km"
_CLIMB_AND_RIDE = [
    Intent(kind="log_session", sport="climbing"),
    Intent(kind="log_session", sport="cycling"),
]
_HOME_15_KM = CyclingExtraction.model_validate(
    {
        "occurred_on": date(2026, 10, 2),
        "date_source": "assumed_today",
        "activities": [{"distance": {"original": "15 km", "value": 15, "unit": "km"}}],
    }
)
_FAILED = LlmCallOutcome.ERROR


class _Setup:
    def __init__(self, *, failing: tuple[str, ...] = (), intents: list[Intent] | None = None):
        self.chat = InMemoryChatRepository()
        self.drafts = InMemoryDraftRepository()
        self.user_id = uuid4()
        self.conversation = ScriptedConversation(
            intents=[intents or _CLIMB_AND_RIDE],
            sport_extractions={"climbing": THREE_6A_TWO_6B, "cycling": _HOME_15_KM},
            sport_outcomes={sport: [_FAILED, LlmCallOutcome.SUCCESS] for sport in failing},
        )
        self.services = reply_services(
            self.chat, drafts=self.drafts, conversation=self.conversation
        )

    async def log(self, text: str = _MIXED) -> ChatMessage:
        return await self.chat.add(
            self.user_id,
            await self.chat.conversation_of(self.user_id),
            ChatRole.USER,
            text,
            FROZEN_NOW,
        )

    async def reply(self, message: ChatMessage) -> list[ReplyEvent]:
        events = await start_reply(
            self.user_id, message.id, timezone="Europe/Warsaw", services=self.services
        )
        return [event async for event in events]

    async def retry(
        self, part_id: UUID, *, user_id: UUID | None = None
    ) -> RetriedDraft | RetryFailed:
        return await retry_draft(
            user_id or self.user_id, part_id, timezone="Europe/Warsaw", services=self.services
        )

    async def failed_part(self, message: ChatMessage) -> ChatMessage:
        """The reply's one failed part, after `reply` saved it."""
        (part,) = [
            part
            for part in await self.chat.list_replies(self.user_id, message)
            if part.template_id == DRAFT_FAILED_TEMPLATE_ID
        ]
        return part


def _done(events: list[ReplyEvent]) -> ReplyDone:
    assert isinstance(events[-1], ReplyDone)
    return events[-1]


def _streamed(events: list[ReplyEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, ReplyChunk))


async def test_a_climb_and_a_ride_make_two_drafts_in_one_reply_in_message_order() -> None:
    setup = _Setup()
    asked = await setup.log()

    done = _done(await setup.reply(asked))

    assert [(part.template_id, part.sports) for part in done.messages] == [
        ("climbing.extract", ("climbing",)),
        ("cycling.extract", ("cycling",)),
    ]
    assert [(draft.sport_id, draft.status, draft.message_id) for draft in done.drafts] == [
        ("climbing", "pending", asked.id),
        ("cycling", "pending", asked.id),
    ]
    assert done.drafts[1].draft.payload == _HOME_15_KM.to_payload().model_dump(mode="json")
    assert setup.drafts.all_of(setup.user_id) == list(done.drafts)


async def test_both_extractions_see_the_whole_message() -> None:
    setup = _Setup()
    asked = await setup.log()

    await setup.reply(asked)

    assert sorted((ctx.message, sport) for ctx, sport in setup.conversation.extracted) == [
        (_MIXED, "climbing"),
        (_MIXED, "cycling"),
    ]


async def test_the_extractions_run_at_once() -> None:
    """The climb's extraction answers only once the ride's has started: run one after the
    other, the reply would never finish (G14: two sports must not double the wait)."""
    setup = _Setup()
    setup.conversation.extract_waits["climbing"] = setup.conversation.extract_started.setdefault(
        "cycling", asyncio.Event()
    )
    asked = await setup.log()

    events = await asyncio.wait_for(setup.reply(asked), timeout=2)

    assert len(_done(events).drafts) == 2


async def test_a_failed_ride_keeps_the_climbing_draft_and_saves_a_failed_part() -> None:
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()

    events = await setup.reply(asked)

    done = _done(events)
    assert [(part.text, part.template_id, part.sports) for part in done.messages] == [
        (DRAFT_READY_TEXT, "climbing.extract", ("climbing",)),
        (DRAFT_FAILED_TEXT, DRAFT_FAILED_TEMPLATE_ID, ("cycling",)),
    ]
    assert [draft.sport_id for draft in done.drafts] == ["climbing"]
    assert [draft.sport_id for draft in setup.drafts.all_of(setup.user_id)] == ["climbing"]
    assert _streamed(events) == f"{DRAFT_READY_TEXT}{PART_BREAK}{DRAFT_FAILED_TEXT}"
    assert setup.chat.all_of(setup.user_id) == [asked, *done.messages]


async def test_a_failed_climb_keeps_its_place_before_the_ride() -> None:
    setup = _Setup(failing=("climbing",))
    asked = await setup.log()

    done = _done(await setup.reply(asked))

    assert [part.template_id for part in done.messages] == [
        DRAFT_FAILED_TEMPLATE_ID,
        "cycling.extract",
    ]
    assert [draft.sport_id for draft in done.drafts] == ["cycling"]


@pytest.mark.parametrize("outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR])
async def test_when_every_extraction_fails_the_reply_fails_and_saves_nothing(
    outcome: LlmCallOutcome,
) -> None:
    setup = _Setup()
    setup.conversation.sport_outcomes = {"climbing": [outcome], "cycling": [outcome]}
    asked = await setup.log()

    events = await setup.reply(asked)

    assert events[-1] == ReplyFailed(outcome=outcome)
    assert setup.chat.all_of(setup.user_id) == [asked]
    assert setup.drafts.all_of(setup.user_id) == []


async def test_a_failed_log_beside_a_greeting_keeps_the_greeting_and_a_failed_part() -> None:
    """Any part that came through is kept; only a reply with nothing to show fails whole."""
    setup = _Setup(
        failing=("climbing",),
        intents=[Intent(kind="chitchat"), Intent(kind="log_session", sport="climbing")],
    )
    asked = await setup.log("hi! bouldered 4 6B")

    done = _done(await setup.reply(asked))

    assert [part.template_id for part in done.messages] == ["chitchat", DRAFT_FAILED_TEMPLATE_ID]
    assert setup.drafts.all_of(setup.user_id) == []


async def test_a_reply_with_a_failed_part_replays_as_saved_with_no_model_call() -> None:
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    first = _done(await setup.reply(asked))
    calls = len(setup.conversation.extracted)

    again = _done(await setup.reply(asked))

    assert (again.messages, again.drafts) == (first.messages, first.drafts)
    assert len(setup.conversation.extracted) == calls


async def test_retry_drafts_the_failed_sport_and_turns_its_part_into_the_intro() -> None:
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    failed = _done(await setup.reply(asked)).messages[1]

    retried = await setup.retry(failed.id)

    assert isinstance(retried, RetriedDraft)
    part = retried.part
    assert (part.id, part.created_at, part.sports) == (failed.id, failed.created_at, ("cycling",))
    assert (part.text, part.template_id) == (DRAFT_READY_TEXT, "cycling.extract")
    assert (retried.draft.sport_id, retried.draft.status, retried.draft.message_id) == (
        "cycling",
        "pending",
        asked.id,
    )
    assert retried.draft.draft.payload == _HOME_15_KM.to_payload().model_dump(mode="json")
    assert await setup.chat.get(setup.user_id, failed.id) == part
    assert [draft.sport_id for draft in setup.drafts.all_of(setup.user_id)] == [
        "climbing",
        "cycling",
    ]


async def test_retry_reads_the_log_as_of_when_it_was_sent() -> None:
    """Like the first extraction, so "yesterday" still means the log's yesterday (ADR-0014)."""
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    await setup.reply(asked)

    await setup.retry((await setup.failed_part(asked)).id)

    context, sport = setup.conversation.extracted[-1]
    assert (sport, context.message, context.now) == ("cycling", _MIXED, asked.created_at)


async def test_a_failed_retry_changes_nothing() -> None:
    setup = _Setup()
    setup.conversation.sport_outcomes = {"cycling": [_FAILED, LlmCallOutcome.TIMEOUT]}
    asked = await setup.log()
    failed = _done(await setup.reply(asked)).messages[1]

    retried = await setup.retry(failed.id)

    assert retried == RetryFailed(part=failed, outcome=LlmCallOutcome.TIMEOUT)
    assert await setup.chat.get(setup.user_id, failed.id) == failed
    assert [draft.sport_id for draft in setup.drafts.all_of(setup.user_id)] == ["climbing"]


async def test_retrying_anything_but_a_failed_part_is_not_found_with_no_call() -> None:
    """The climbing intro, the athlete's own log and an unknown ID have nothing to retry."""
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    climb = _done(await setup.reply(asked)).messages[0]
    calls = len(setup.conversation.extracted)

    for part_id in (climb.id, asked.id, uuid4()):
        with pytest.raises(FailedPartNotFoundError):
            await setup.retry(part_id)

    assert len(setup.conversation.extracted) == calls


async def test_a_second_retry_after_one_came_through_is_not_found() -> None:
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    await setup.reply(asked)
    failed = await setup.failed_part(asked)
    await setup.retry(failed.id)

    with pytest.raises(FailedPartNotFoundError):
        await setup.retry(failed.id)

    assert len(setup.drafts.all_of(setup.user_id)) == 2


async def test_two_retries_at_once_leave_one_draft() -> None:
    setup = _Setup()
    setup.conversation.sport_outcomes = {"cycling": [_FAILED, LlmCallOutcome.SUCCESS]}
    asked = await setup.log()
    await setup.reply(asked)
    failed = await setup.failed_part(asked)

    outcomes = await asyncio.gather(
        setup.retry(failed.id), setup.retry(failed.id), return_exceptions=True
    )

    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "FailedPartNotFoundError",
        "RetriedDraft",
    ]
    assert [draft.sport_id for draft in setup.drafts.all_of(setup.user_id)] == [
        "climbing",
        "cycling",
    ]


async def test_another_users_failed_part_is_not_found_and_nothing_is_called() -> None:
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    await setup.reply(asked)
    failed = await setup.failed_part(asked)
    calls = len(setup.conversation.extracted)

    with pytest.raises(FailedPartNotFoundError):
        await setup.retry(failed.id, user_id=uuid4())

    assert len(setup.conversation.extracted) == calls
    assert await setup.chat.get(setup.user_id, failed.id) == failed


async def test_a_failed_part_of_a_log_past_its_retention_is_not_found() -> None:
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    await setup.reply(asked)
    failed = await setup.failed_part(asked)
    setup.chat.conversations.rows.clear()

    with pytest.raises(MessageNotFoundError):
        await setup.retry(failed.id)


class _FailingDrafts(InMemoryDraftRepository):
    async def add(self, *args: object, **kwargs: object) -> StoredDraft:
        raise RuntimeError("the database went away")


async def test_a_retry_whose_draft_is_not_stored_puts_the_failed_part_back() -> None:
    setup = _Setup(failing=("cycling",))
    asked = await setup.log()
    await setup.reply(asked)
    failed = await setup.failed_part(asked)
    setup.services = replace(setup.services, drafts=_FailingDrafts())

    with pytest.raises(RuntimeError):
        await setup.retry(failed.id)

    assert await setup.chat.get(setup.user_id, failed.id) == failed


async def test_a_failed_part_with_no_log_before_it_is_not_found() -> None:
    setup = _Setup()
    orphan = await setup.chat.add(
        setup.user_id,
        await setup.chat.conversation_of(setup.user_id),
        ChatRole.ASSISTANT,
        DRAFT_FAILED_TEXT,
        FROZEN_NOW,
        sports=("cycling",),
        template_id=DRAFT_FAILED_TEMPLATE_ID,
        template_version=1,
    )

    with pytest.raises(FailedPartNotFoundError):
        await setup.retry(orphan.id)

    assert setup.conversation.extracted == []
