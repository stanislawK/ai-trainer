"""The sport glyph on replies (PRD-0003 F15, ADR-0006, ADR-0019, ticket #84): an assistant
reply's avatar is its one sport's icon from `SportRegistry`, else the app mark."""

import logging
import re
from collections.abc import Sequence
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.web.icons import ICONS_DIR, render_icon
from ai_trainer.web.templating import TEMPLATES_DIR, build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryChatRepository,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_AVATAR = re.compile(r'<span data-avatar="([^"]+)"')


def _client() -> tuple[TestClient, UUID, InMemoryChatRepository]:
    app = FastAPI()
    repository = include_chat(app, build_templates())
    user = sign_in_as(app)
    return TestClient(app), user.id, repository


async def _reply(chat: InMemoryChatRepository, user_id: UUID, sports: Sequence[str]) -> None:
    await chat.add(user_id, ChatRole.USER, "hi", FROZEN_NOW)
    await chat.add(user_id, ChatRole.ASSISTANT, "Hey!", FROZEN_NOW, sports=sports)


def _svg(icon_name: str) -> str:
    return str(render_icon(icon_name, "size-4"))


async def test_a_chitchat_reply_shows_the_app_mark() -> None:
    client, user_id, chat = _client()
    await _reply(chat, user_id, [])

    page = client.get("/").text

    assert _AVATAR.findall(page) == ["app-mark"]
    assert _svg("trending-up") in page


async def test_a_reply_about_cycling_shows_the_bike() -> None:
    client, user_id, chat = _client()
    await _reply(chat, user_id, ["cycling"])

    page = client.get("/").text

    assert _AVATAR.findall(page) == ["bike"]
    assert _svg("bike") in page


async def test_a_reply_about_climbing_and_gym_shows_the_app_mark() -> None:
    client, user_id, chat = _client()
    await _reply(chat, user_id, ["climbing", "gym"])

    assert _AVATAR.findall(client.get("/").text) == ["app-mark"]


async def test_an_unknown_stored_sport_shows_the_app_mark_and_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, user_id, chat = _client()
    await _reply(chat, user_id, ["curling"])

    with caplog.at_level(logging.WARNING):
        response = client.get("/")

    assert response.status_code == 200
    assert _AVATAR.findall(response.text) == ["app-mark"]
    assert [record.getMessage() for record in caplog.records if "curling" in record.getMessage()]


async def test_a_streamed_reply_and_the_history_render_the_same_avatar() -> None:
    client, user_id, chat = _client()
    asked = await chat.add(user_id, ChatRole.USER, "hi", FROZEN_NOW)

    streamed = client.get(f"/messages/{asked.id}/reply").text
    history = client.get("/").text

    assert _AVATAR.findall(streamed) == _AVATAR.findall(history) == ["app-mark"]


async def test_a_streamed_cycling_reply_and_the_history_both_show_the_bike() -> None:
    conversation = ScriptedConversation(intents=[[Intent(kind="log_session", sport="cycling")]])
    app = FastAPI()
    chat = include_chat(app, build_templates(), conversation=conversation)
    user = sign_in_as(app)
    client = TestClient(app)
    asked = await chat.add(user.id, ChatRole.USER, "log my ride", FROZEN_NOW)

    streamed = client.get(f"/messages/{asked.id}/reply").text
    history = client.get("/").text

    assert _AVATAR.findall(streamed) == _AVATAR.findall(history) == ["bike"]


def test_no_chat_template_hard_codes_a_sport_icon() -> None:
    sport_icons = {plugin.icon for plugin in default_sport_registry().all()}
    pattern = re.compile(rf"""icon\(\s*["']({"|".join(sport_icons)})["']""")

    offenders = [
        path.name
        for path in (TEMPLATES_DIR / "partials" / "chat").glob("*.html")
        if pattern.search(path.read_text())
    ]

    assert offenders == []


def test_the_app_mark_icon_is_vendored() -> None:
    assert (ICONS_DIR / "trending-up.svg").is_file()
