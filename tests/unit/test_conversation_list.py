"""The conversation list (PRD-0008 B25, B26, F17, ADR-0021, ticket #213): the athlete's
conversations inside the retention window, newest first by last activity, each with its title
and last-activity time in the athlete's timezone, as a drawer on a phone and a sidebar section
on desktop. Picking one opens it; sending a message moves it to the top; the open one is
marked; another athlete's never shows."""

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.conversations import LastActive, last_active
from ai_trainer.web.templating import build_templates
from tests.chat_support import FROZEN_NOW, InMemoryChatRepository, include_chat, sign_in_as

_HTMX = {"HX-Request": "true"}
_LISTS = ("phone", "desktop")
_WARSAW = "Europe/Warsaw"


def _user(timezone: str = "UTC") -> User:
    return User(
        id=uuid4(),
        sub=f"sub-{uuid4()}",
        email="athlete@example.com",
        name="Ada Lovelace",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
        timezone=timezone,
    )


def _client(
    user: User | None = None, chat: InMemoryChatRepository | None = None
) -> tuple[TestClient, User, InMemoryChatRepository]:
    app = FastAPI()
    repository = include_chat(app, build_templates(), chat)
    return TestClient(app), sign_in_as(app, user or _user()), repository


async def _conversation(
    chat: InMemoryChatRepository, user_id: UUID, title: str, when: datetime
) -> UUID:
    conversation = await chat.conversations.create(user_id, title, when)
    await chat.add(user_id, conversation.id, ChatRole.USER, title, when)
    return conversation.id


def _list(html: str, variant: str) -> str:
    markup: str
    (markup,) = re.findall(
        rf'<div id="conversation-list-{variant}"[^>]*>.*?<!-- /conversation-list -->', html, re.S
    )
    return markup


def _rows(html: str, variant: str = "phone") -> list[tuple[str, str, str]]:
    """Each row of one list as (id, title, time)."""
    return re.findall(
        r'data-conversation-row="([^"]+)".*?<span[^>]*data-title[^>]*>([^<]*)</span>'
        r"<span[^>]*data-time[^>]*>([^<]*)</span>",
        _list(html, variant),
        re.S,
    )


def _current(html: str, variant: str) -> list[str]:
    return re.findall(
        r'data-conversation-row="([^"]+)"[^>]*aria-current="true"', _list(html, variant)
    )


async def test_lists_conversations_newest_first_with_title_and_time() -> None:
    client, user, chat = _client()
    older = await _conversation(
        chat, user.id, "Board session, 90 min", FROZEN_NOW - timedelta(days=10)
    )
    newest = await _conversation(
        chat, user.id, "Hades 8a redpoint", FROZEN_NOW - timedelta(hours=1)
    )
    middle = await _conversation(chat, user.id, "Leg day", FROZEN_NOW - timedelta(days=1))

    response = client.get("/c/new")

    for variant in _LISTS:
        assert _rows(response.text, variant) == [
            (str(newest), "Hades 8a redpoint", "08:30"),
            (str(middle), "Leg day", "Yesterday"),
            (str(older), "Board session, 90 min", "22 Sep"),
        ]


async def test_list_times_are_in_the_athletes_timezone() -> None:
    client, user, chat = _client(_user(_WARSAW))
    await _conversation(chat, user.id, "Evening ride", FROZEN_NOW - timedelta(hours=1))

    response = client.get("/c/new")

    for variant in _LISTS:
        [(_, title, time)] = _rows(response.text, variant)
        assert (title, time) == ("Evening ride", "10:30")


async def test_an_untitled_conversation_still_has_a_row_label() -> None:
    client, user, chat = _client()
    conversation = await chat.conversations.create(user.id, None, FROZEN_NOW)

    response = client.get("/c/new")

    assert _rows(response.text) == [(str(conversation.id), "Untitled conversation", "09:30")]


async def test_opening_a_listed_conversation_shows_its_messages() -> None:
    client, user, chat = _client()
    picked = await _conversation(chat, user.id, "Sent Hades today", FROZEN_NOW - timedelta(days=2))
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW - timedelta(hours=1))
    page = client.get("/")
    (row,) = re.findall(
        rf'<a[^>]*data-conversation-row="{picked}"[^>]*>', _list(page.text, "phone")
    )
    assert f'hx-get="/c/{picked}"' in row
    assert 'hx-target="#chat-content"' in row
    assert 'hx-push-url="true"' in row

    response = client.get(f"/c/{picked}", headers=_HTMX)

    assert response.status_code == 200
    assert "Sent Hades today" in response.text.split("<!-- /chat-content -->")[0]
    assert "Leg day" not in response.text.split("<!-- /chat-content -->")[0]


async def test_an_htmx_chat_swap_rerenders_both_lists_out_of_band() -> None:
    client, user, chat = _client()
    picked = await _conversation(chat, user.id, "Sent Hades today", FROZEN_NOW - timedelta(days=2))

    response = client.get(f"/c/{picked}", headers=_HTMX)

    for variant in _LISTS:
        assert re.search(
            rf'<div id="conversation-list-{variant}"[^>]*hx-swap-oob="true"', response.text
        )
        assert _current(response.text, variant) == [str(picked)]


async def test_a_full_page_load_renders_the_lists_in_place_not_out_of_band() -> None:
    client, user, chat = _client()
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW)

    response = client.get("/")

    assert 'hx-swap-oob="true"' not in response.text


async def test_sending_a_message_moves_its_conversation_to_the_top() -> None:
    client, user, chat = _client()
    older = await _conversation(chat, user.id, "Board session", FROZEN_NOW - timedelta(days=3))
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW - timedelta(hours=2))

    response = client.post(
        "/messages",
        data={"message": "One more thing", "conversation_id": str(older)},
        headers=_HTMX,
    )

    assert response.status_code == 200
    for variant in _LISTS:
        assert re.search(
            rf'<div id="conversation-list-{variant}"[^>]*hx-swap-oob="true"', response.text
        )
        rows = _rows(response.text, variant)
        assert rows[0][0] == str(older)
        assert rows[0][2] == "09:30"
        assert _current(response.text, variant) == [str(older)]


async def test_a_first_message_adds_its_new_conversation_to_the_top_marked() -> None:
    client, user, chat = _client()
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW - timedelta(hours=2))

    response = client.post("/messages", data={"message": "Sent Hades 8a"}, headers=_HTMX)

    created, _ = await chat.conversations.list_recent(user.id, 10)
    for variant in _LISTS:
        rows = _rows(response.text, variant)
        assert [row[:2] for row in rows] == [
            (str(created.id), "Sent Hades 8a"),
            (rows[1][0], "Leg day"),
        ]
        assert _current(response.text, variant) == [str(created.id)]


async def test_a_rejected_message_does_not_rerender_the_list() -> None:
    client, user, chat = _client()
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW)

    response = client.post("/messages", data={"message": "   "}, headers=_HTMX)

    assert response.status_code == 422
    assert "conversation-list-" not in response.text


async def test_the_open_conversation_is_marked_in_both_lists() -> None:
    client, user, chat = _client()
    opened = await _conversation(chat, user.id, "Board session", FROZEN_NOW - timedelta(days=1))
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW)

    response = client.get(f"/c/{opened}")

    for variant in _LISTS:
        assert _current(response.text, variant) == [str(opened)]
        (row,) = re.findall(
            rf'<a[^>]*data-conversation-row="{opened}"[^>]*>', _list(response.text, variant)
        )
        assert {"lg", "lg-clear"} <= set(re.search(r'class="([^"]*)"', row)[1].split())  # type: ignore[index]  # one row matched above


async def test_root_marks_the_latest_conversation_it_opens() -> None:
    client, user, chat = _client()
    await _conversation(chat, user.id, "Board session", FROZEN_NOW - timedelta(days=1))
    latest = await _conversation(chat, user.id, "Leg day", FROZEN_NOW)

    response = client.get("/")

    assert _current(response.text, "phone") == [str(latest)]


async def test_a_new_conversation_marks_no_row() -> None:
    client, user, chat = _client()
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW)

    response = client.get("/c/new")

    for variant in _LISTS:
        assert _current(response.text, variant) == []


async def test_a_conversation_last_active_exactly_at_the_retention_boundary_is_listed() -> None:
    client, user, chat = _client()
    boundary = await _conversation(chat, user.id, "Day fourteen", FROZEN_NOW - timedelta(days=14))
    # Already expired, so it can't take a message: the conversation row alone.
    await chat.conversations.create(
        user.id, "Past the window", FROZEN_NOW - timedelta(days=14, seconds=1)
    )

    response = client.get("/c/new")

    for variant in _LISTS:
        assert [row[:2] for row in _rows(response.text, variant)] == [
            (str(boundary), "Day fourteen")
        ]


async def test_another_athletes_conversations_never_appear_in_the_list() -> None:
    chat = InMemoryChatRepository()
    client, athlete_a, _ = _client(chat=chat)
    athlete_b = _user()
    own = await _conversation(chat, athlete_a.id, "Mine", FROZEN_NOW - timedelta(hours=1))
    theirs = await _conversation(chat, athlete_b.id, "Theirs", FROZEN_NOW)

    for path in ("/", "/c/new", f"/c/{own}"):
        response = client.get(path)
        for variant in _LISTS:
            assert [row[0] for row in _rows(response.text, variant)] == [str(own)]
        assert str(theirs) not in response.text
        assert "Theirs" not in response.text

    assert client.get(f"/c/{theirs}").status_code == 404
    assert client.get(f"/c/{theirs}", headers=_HTMX).status_code == 404


async def test_no_conversations_shows_the_empty_state_with_the_retention_window() -> None:
    client, _, _ = _client()

    response = client.get("/")

    for variant in _LISTS:
        markup = _list(response.text, variant)
        assert "No conversations yet" in markup
        assert "Conversations from the last 14 days show up here." in markup
        assert "data-conversation-row" not in markup


async def test_the_phone_list_names_the_retention_window() -> None:
    client, user, chat = _client()
    await _conversation(chat, user.id, "Leg day", FROZEN_NOW)

    response = client.get("/")

    assert "Last 14 days" in _list(response.text, "phone")


def test_each_list_opens_with_new_conversation_as_its_tinted_action() -> None:
    client, _, _ = _client()

    response = client.get("/")

    for variant in _LISTS:
        (action,) = re.findall(r"<a[^>]*data-new-conversation[^>]*>", _list(response.text, variant))
        assert 'hx-get="/c/new"' in action
        assert {"lg", "lg-tinted"} <= set(re.search(r'class="([^"]*)"', action)[1].split())  # type: ignore[index]  # the anchor has a class


# The time label (mock "Conversation list v3"): today's HH:MM, then Yesterday, the weekday within
# this Monday-to-Sunday week, then the day and month. FROZEN_NOW is Friday 2 October 2026.


@pytest.mark.parametrize(
    ("when", "kind"),
    [
        (datetime(2026, 10, 2, 0, 30, tzinfo=UTC), "today"),
        (datetime(2026, 10, 1, 23, 30, tzinfo=UTC), "yesterday"),
        (datetime(2026, 9, 28, 0, 0, tzinfo=UTC), "weekday"),
        (datetime(2026, 9, 27, 23, 59, tzinfo=UTC), "date"),
    ],
    ids=["today-after-midnight", "yesterday-before-midnight", "monday-this-week", "last-sunday"],
)
def test_last_active_kind(when: datetime, kind: str) -> None:
    assert last_active(when, now=FROZEN_NOW, timezone="UTC") == LastActive(kind, when)  # type: ignore[arg-type]  # the ids above are the Literal's values


def test_last_active_reads_the_day_in_the_athletes_timezone() -> None:
    # 22:30 UTC on Thursday is 00:30 on Friday in Warsaw (CEST, UTC+2): today, not yesterday.
    label = last_active(datetime(2026, 10, 1, 22, 30, tzinfo=UTC), now=FROZEN_NOW, timezone=_WARSAW)

    assert label.kind == "today"
    assert (label.local.hour, label.local.minute) == (0, 30)


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (datetime(2026, 10, 2, 8, 5, tzinfo=UTC), "08:05"),
        (datetime(2026, 10, 1, 8, 5, tzinfo=UTC), "Yesterday"),
        (datetime(2026, 9, 29, 8, 5, tzinfo=UTC), "Tue"),
        (datetime(2026, 9, 3, 8, 5, tzinfo=UTC), "3 Sep"),
    ],
)
def test_the_time_label_renders_each_kind(local: datetime, expected: str) -> None:
    env = build_templates().env
    label = last_active(local, now=FROZEN_NOW, timezone="UTC")

    macro = env.get_template("partials/chat/conversations.html").module.time_label  # type: ignore[attr-defined]  # Jinja exports macros on the module

    assert str(macro(label)).strip() == expected


async def test_a_history_restore_gets_the_full_page_with_its_lists() -> None:
    """htmx 4 restores Back/Forward with an ajax GET that also carries `HX-Request` and swaps
    the whole body, so it needs the page, sidebar and lists included, not the chat partial."""
    client, user, chat = _client()
    opened = await _conversation(chat, user.id, "Board session", FROZEN_NOW - timedelta(days=1))

    response = client.get(f"/c/{opened}", headers={**_HTMX, "HX-History-Restore-Request": "true"})

    assert "<html" in response.text
    assert 'hx-swap-oob="true"' not in response.text
    for variant in _LISTS:
        assert _current(response.text, variant) == [str(opened)]
