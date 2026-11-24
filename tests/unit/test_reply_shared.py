"""The reply context carries the athlete's limits from their profile (PRD 0013 B46, ADR-0028
Consequences, ticket #629), read for the acting user only."""

from uuid import uuid4

from ai_trainer.application.reply.shared import reply_context
from ai_trainer.domain.athlete_profile import AthleteProfile
from ai_trainer.domain.chat import ChatRole
from tests.chat_support import FROZEN_NOW, InMemoryChatRepository, InMemoryProfile, reply_services


async def test_reply_context_reads_the_athletes_limits() -> None:
    chat, user_id, other = InMemoryChatRepository(), uuid4(), uuid4()
    message = await chat.add(
        user_id, await chat.conversation_of(user_id), ChatRole.USER, "Bench?", FROZEN_NOW
    )
    profile = InMemoryProfile(
        profiles={
            user_id: AthleteProfile(limits="sore left shoulder"),
            other: AthleteProfile(limits="someone else's knee"),
        }
    )

    context = await reply_context(
        user_id, message, "Europe/Warsaw", reply_services(chat, profile=profile)
    )

    assert context.limits == "sore left shoulder"


async def test_reply_context_without_profile_has_no_limits() -> None:
    chat, user_id = InMemoryChatRepository(), uuid4()
    message = await chat.add(
        user_id, await chat.conversation_of(user_id), ChatRole.USER, "Bench?", FROZEN_NOW
    )

    context = await reply_context(user_id, message, "Europe/Warsaw", reply_services(chat))

    assert context.limits is None
