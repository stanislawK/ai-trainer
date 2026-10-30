"""`GET /` and `POST /messages` (PRD-0003 F1, B13, ADR-0008, ticket #78): the chat page, and a
message appended to it through htmx."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_trainer.application.chat import MAX_MESSAGE_LENGTH
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.templating import STATIC_DIR, build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryChatRepository,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}


def _user(name: str | None = "Ada Lovelace", timezone: str = "UTC") -> User:
    return User(
        id=uuid4(),
        sub=f"sub-{uuid4()}",
        email="athlete@example.com",
        name=name,
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
    signed_in = sign_in_as(app, user)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return TestClient(app), signed_in, repository


def test_get_root_returns_full_page_with_stylesheet_link() -> None:
    client, _, _ = _client()

    response = client.get("/")

    assert response.status_code == 200
    assert "<html" in response.text
    assert '<link rel="stylesheet" href="/static/css/app.css">' in response.text


def test_stylesheet_asset_is_servable_when_compiled() -> None:
    """app.css is a gitignored build artifact (`make css` / the Docker css-builder
    stage, ADR-0012) — a fresh checkout has no network access in pytest (ADR-0013),
    so this skips rather than fails when nobody has compiled it yet; it runs for
    real once `make css` (or the Dockerfile) has produced the file."""
    if not (STATIC_DIR / "css" / "app.css").is_file():
        pytest.skip("static/css/app.css not built — run `make css` first")

    client, _, _ = _client()

    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_get_root_with_hx_request_returns_partial_without_html_element() -> None:
    client, _, _ = _client()

    response = client.get("/", headers=_HTMX)

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "<!doctype" not in response.text.lower()
    assert 'id="chat-composer"' in response.text


def test_get_root_references_local_htmx_script_with_no_external_origin() -> None:
    client, _, _ = _client()

    response = client.get("/")

    assert '<script src="/static/vendor/htmx-4.0.0.min.js">' in response.text
    assert "http://" not in response.text
    assert "https://" not in response.text


def test_vendored_htmx_file_exists_and_is_served() -> None:
    client, _, _ = _client()

    assert (STATIC_DIR / "vendor" / "htmx-4.0.0.min.js").is_file()

    response = client.get("/static/vendor/htmx-4.0.0.min.js")

    assert response.status_code == 200


def test_htmx_vendor_file_is_not_an_empty_placeholder() -> None:
    contents = (STATIC_DIR / "vendor" / "htmx-4.0.0.min.js").read_text()

    assert len(contents) > 1000
    assert "htmx" in contents.lower()


def test_web_dir_has_the_required_template_and_static_layout() -> None:
    web_dir = Path(STATIC_DIR).parent

    assert (web_dir / "templates" / "pages").is_dir()
    assert (web_dir / "templates" / "partials").is_dir()
    assert (web_dir / "templates" / "components").is_dir()
    assert (web_dir / "static").is_dir()


def test_chat_page_is_the_chat_nav_variant_without_a_dock() -> None:
    client, _, _ = _client()

    response = client.get("/")

    assert 'class="dock lg:hidden' not in response.text
    assert 'popovertarget="sections-menu"' in response.text


def test_empty_chat_shows_a_greeting_with_the_first_name_and_the_part_of_the_day() -> None:
    client, _, _ = _client(_user("Ada Lovelace"))

    response = client.get("/")

    assert "Good morning, Ada" in response.text
    assert "Tell me what you trained" in response.text


def test_greeting_follows_the_athletes_timezone_not_utc() -> None:
    # 09:30 UTC is 18:30 in Tokyo.
    client, _, _ = _client(_user(timezone="Asia/Tokyo"))

    assert "Good evening" in client.get("/").text


@pytest.mark.parametrize(
    ("timezone", "greeting"),
    [
        ("UTC", "Good morning"),
        ("Europe/Warsaw", "Good morning"),
        ("Asia/Kolkata", "Good afternoon"),
    ],
)
def test_greeting_names_morning_afternoon_and_evening(timezone: str, greeting: str) -> None:
    client, _, _ = _client(_user(timezone=timezone))

    assert greeting in client.get("/").text


@pytest.mark.parametrize("name", [None, "", "   "])
def test_greeting_has_no_name_when_the_account_has_none(name: str | None) -> None:
    client, _, _ = _client(_user(name))

    assert "Good morning</h2>" in client.get("/").text


def test_the_empty_state_hides_itself_once_a_message_exists() -> None:
    client, _, _ = _client()

    assert "group-has-[.chat-message]/chat:hidden" in client.get("/").text


async def test_history_lists_the_athletes_messages_oldest_first() -> None:
    client, user, repository = _client()
    await repository.add(
        user.id, await repository.conversation_of(user.id), ChatRole.USER, "first one", FROZEN_NOW
    )
    await repository.add(
        user.id,
        await repository.conversation_of(user.id),
        ChatRole.USER,
        "second one",
        FROZEN_NOW + timedelta(minutes=1),
    )

    text = client.get("/").text

    assert text.index("first one") < text.index("second one")


async def test_history_shows_only_the_latest_fifty() -> None:
    client, user, repository = _client()
    for index in range(55):
        await repository.add(
            user.id,
            await repository.conversation_of(user.id),
            ChatRole.USER,
            f"msg-{index:02d}-end",
            datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=index),
        )

    text = client.get("/").text

    assert "msg-04-end" not in text
    assert "msg-05-end" in text
    assert "msg-54-end" in text


async def test_user_b_never_sees_user_as_messages() -> None:
    repository = InMemoryChatRepository()
    client_a, user_a, _ = _client(chat=repository)
    client_b, _, _ = _client(chat=repository)
    await repository.add(
        user_a.id,
        await repository.conversation_of(user_a.id),
        ChatRole.USER,
        "for a only",
        FROZEN_NOW,
    )

    assert "for a only" in client_a.get("/").text
    assert "for a only" not in client_b.get("/").text


def test_sending_hello_returns_the_message_and_a_fresh_composer_and_stores_it() -> None:
    client, user, repository = _client()

    response = client.post("/messages", data={"message": "hello"}, headers=_HTMX)

    assert response.status_code == 200
    assert 'hx-swap-oob="beforeend:#chat-history"' in response.text
    assert ">hello</div>" in response.text
    assert 'id="chat-composer"' in response.text
    assert [(owner, message.text) for owner, message in repository.rows] == [(user.id, "hello")]


def test_a_sent_message_is_in_the_history_after_a_reload() -> None:
    client, _, _ = _client()
    client.post("/messages", data={"message": "hello"}, headers=_HTMX)

    assert ">hello</div>" in client.get("/").text


def test_a_message_is_stored_for_the_session_user_whatever_the_form_says() -> None:
    client, user, repository = _client()

    client.post("/messages", data={"message": "hi", "user_id": str(uuid4())}, headers=_HTMX)

    assert [owner for owner, _ in repository.rows] == [user.id]


def test_message_text_is_escaped() -> None:
    client, _, _ = _client()

    response = client.post("/messages", data={"message": "<b>bold</b>"}, headers=_HTMX)

    assert "<b>bold</b>" not in response.text
    assert "&lt;b&gt;bold&lt;/b&gt;" in response.text


@pytest.mark.parametrize("text", ["", "   ", "\n\t\n"])
def test_an_empty_or_whitespace_message_is_refused_with_an_inline_error_and_not_stored(
    text: str,
) -> None:
    client, _, repository = _client()

    response = client.post("/messages", data={"message": text}, headers=_HTMX)

    assert response.status_code == 422
    assert "Write a message first." in response.text
    assert "hx-swap-oob" not in response.text
    assert repository.rows == []


def test_a_missing_message_field_is_treated_as_empty() -> None:
    client, _, repository = _client()

    response = client.post("/messages", data={}, headers=_HTMX)

    assert response.status_code == 422
    assert repository.rows == []


def test_a_message_over_4000_characters_shows_an_inline_error_and_keeps_the_text() -> None:
    client, _, repository = _client()
    too_long = "x" * (MAX_MESSAGE_LENGTH + 1)

    response = client.post("/messages", data={"message": too_long}, headers=_HTMX)

    assert response.status_code == 422
    assert "That message is too long. Keep it under 4000 characters." in response.text
    assert too_long in response.text
    assert 'aria-invalid="true"' in response.text
    assert repository.rows == []


def test_a_message_of_exactly_4000_characters_is_sent() -> None:
    client, _, repository = _client()

    response = client.post("/messages", data={"message": "x" * MAX_MESSAGE_LENGTH}, headers=_HTMX)

    assert response.status_code == 200
    assert len(repository.rows) == 1


# Conversations (PRD-0004 B24, ADR-0021, ticket #146).


def _hidden_conversation(conversation_id: UUID | str) -> str:
    return f'<input type="hidden" name="conversation_id" value="{conversation_id}">'


async def test_root_opens_the_most_recently_active_conversation() -> None:
    client, user, repository = _client()
    older = await repository.start(user.id, "older")
    newer = await repository.start(user.id, "newer")
    await repository.add(user.id, older, ChatRole.USER, "in older", FROZEN_NOW)
    await repository.add(user.id, newer, ChatRole.USER, "in newer", FROZEN_NOW)
    await repository.conversations.touch(user.id, older, FROZEN_NOW + timedelta(minutes=5))

    text = client.get("/").text

    assert "in older" in text
    assert "in newer" not in text
    assert _hidden_conversation(older) in text


def test_root_with_no_conversation_shows_an_empty_chat_whose_composer_starts_one() -> None:
    client, _, _ = _client()

    text = client.get("/").text

    assert 'class="chat-message' not in text
    assert _hidden_conversation("") in text


async def test_a_conversation_opens_at_its_own_address_with_only_its_messages() -> None:
    client, user, repository = _client()
    shown = await repository.start(user.id, "shown")
    other = await repository.start(user.id, "other")
    await repository.add(user.id, shown, ChatRole.USER, "in shown", FROZEN_NOW)
    await repository.add(user.id, other, ChatRole.USER, "in other", FROZEN_NOW)
    await repository.conversations.touch(user.id, other, FROZEN_NOW + timedelta(minutes=5))

    response = client.get(f"/c/{shown}")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "in shown" in response.text
    assert "in other" not in response.text
    assert _hidden_conversation(shown) in response.text


async def test_a_conversation_with_hx_request_returns_the_chat_partial() -> None:
    client, user, repository = _client()
    shown = await repository.start(user.id)

    response = client.get(f"/c/{shown}", headers=_HTMX)

    assert response.status_code == 200
    assert "<html" not in response.text
    assert _hidden_conversation(shown) in response.text


async def test_another_users_conversation_is_not_found() -> None:
    repository = InMemoryChatRepository()
    _, user_a, _ = _client(chat=repository)
    client_b, _, _ = _client(chat=repository)
    theirs = await repository.start(user_a.id)
    await repository.add(user_a.id, theirs, ChatRole.USER, "for a only", FROZEN_NOW)

    response = client_b.get(f"/c/{theirs}")

    assert response.status_code == 404
    assert "for a only" not in response.text


@pytest.mark.parametrize(
    "conversation_id", [str(uuid4()), "not-a-uuid"], ids=["unknown", "malformed"]
)
def test_an_unknown_conversation_is_not_found(conversation_id: str) -> None:
    client, _, _ = _client()

    assert client.get(f"/c/{conversation_id}").status_code == 404


async def test_sending_from_a_conversation_adds_to_it_and_moves_its_last_activity() -> None:
    client, user, repository = _client()
    thread = await repository.conversations.create(
        user.id, "earlier", FROZEN_NOW - timedelta(days=1)
    )

    response = client.post(
        "/messages", data={"message": "more", "conversation_id": str(thread.id)}, headers=_HTMX
    )

    assert response.status_code == 200
    assert [message.conversation_id for _, message in repository.rows] == [thread.id]
    assert _hidden_conversation(thread.id) in response.text
    touched = await repository.conversations.get(user.id, thread.id)
    assert touched is not None
    assert touched.last_message_at == FROZEN_NOW


async def test_a_first_message_starts_a_conversation_and_the_composer_keeps_it() -> None:
    client, user, repository = _client()

    response = client.post(
        "/messages", data={"message": "Rode 40 km", "conversation_id": ""}, headers=_HTMX
    )

    [conversation] = await repository.conversations.list_recent(user.id, 10)
    assert conversation.title == "Rode 40 km"
    assert _hidden_conversation(conversation.id) in response.text


@pytest.mark.parametrize("theirs", [True, False])
async def test_sending_to_another_users_or_an_unknown_conversation_is_not_found(
    theirs: bool,
) -> None:
    repository = InMemoryChatRepository()
    client, _, _ = _client(chat=repository)
    target = await repository.start(uuid4()) if theirs else uuid4()

    response = client.post(
        "/messages", data={"message": "hi", "conversation_id": str(target)}, headers=_HTMX
    )

    assert response.status_code == 404
    assert repository.rows == []


def test_sending_to_a_malformed_conversation_id_is_not_found() -> None:
    client, _, repository = _client()

    response = client.post(
        "/messages", data={"message": "hi", "conversation_id": "nope"}, headers=_HTMX
    )

    assert response.status_code == 404
    assert repository.rows == []


async def test_a_refused_message_keeps_the_composer_on_its_conversation() -> None:
    client, user, repository = _client()
    thread = await repository.start(user.id)

    response = client.post(
        "/messages", data={"message": " ", "conversation_id": str(thread)}, headers=_HTMX
    )

    assert response.status_code == 422
    assert _hidden_conversation(thread) in response.text


# A new conversation (PRD-0004 B24, F16, ADR-0021, ticket #147).


async def test_new_conversation_is_an_empty_chat_with_chips_and_a_composer_that_starts_one() -> (
    None
):
    client, user, repository = _client()
    older = await repository.start(user.id, "older")
    await repository.add(user.id, older, ChatRole.USER, "in older", FROZEN_NOW)

    text = client.get("/c/new").text

    assert "in older" not in text
    assert 'class="chat-message' not in text
    assert "data-chat-chips" in text
    assert _hidden_conversation("") in text


async def test_root_shows_the_latest_conversation_when_it_was_active_today() -> None:
    client, user, repository = _client()
    today = await repository.start(user.id, "today")
    await repository.add(user.id, today, ChatRole.USER, "in today", FROZEN_NOW)

    text = client.get("/").text

    assert "in today" in text
    assert _hidden_conversation(today) in text


async def test_root_is_the_empty_chat_when_the_latest_conversation_is_from_an_earlier_day() -> None:
    client, user, repository = _client()
    earlier = await repository.conversations.create(
        user.id, "yesterday", FROZEN_NOW - timedelta(days=1)
    )
    await repository.add(
        user.id, earlier.id, ChatRole.USER, "in yesterday", FROZEN_NOW - timedelta(days=1)
    )

    text = client.get("/").text

    assert "in yesterday" not in text
    assert 'class="chat-message' not in text
    assert "data-chat-chips" in text
    assert _hidden_conversation("") in text
    assert len(await repository.conversations.list_recent(user.id, 10)) == 1


async def test_the_day_boundary_follows_the_athletes_timezone() -> None:
    """FROZEN_NOW is 09:30 UTC on 2 Oct. Activity at 21:00 UTC on 1 Oct is the same UTC-day
    neighbour, but in Pacific/Kiritimati (UTC+14) it is 11:00 on 2 Oct, today; in UTC it is
    yesterday."""
    activity = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)
    for zone, shown in (("Pacific/Kiritimati", True), ("UTC", False)):
        client, user, repository = _client(_user(timezone=zone))
        thread = await repository.conversations.create(user.id, "t", activity)
        await repository.add(user.id, thread.id, ChatRole.USER, "in thread", activity)

        assert ("in thread" in client.get("/").text) is shown


async def test_an_earlier_days_conversation_stays_listed_and_opens_with_its_messages() -> None:
    client, user, repository = _client()
    yesterday = FROZEN_NOW - timedelta(days=1)
    earlier = await repository.conversations.create(user.id, "Yesterday's ride", yesterday)
    await repository.add(user.id, earlier.id, ChatRole.USER, "in yesterday", yesterday)

    listed = client.get("/").text
    opened = client.get(f"/c/{earlier.id}")

    assert f'href="/c/{earlier.id}"' in listed
    assert opened.status_code == 200
    assert "in yesterday" in opened.text


async def test_the_first_message_from_the_empty_chat_starts_a_new_conversation() -> None:
    client, user, repository = _client()
    yesterday = FROZEN_NOW - timedelta(days=1)
    earlier = await repository.conversations.create(user.id, "yesterday", yesterday)
    await repository.add(user.id, earlier.id, ChatRole.USER, "old turn", yesterday)
    assert "old turn" not in client.get("/").text

    client.post("/messages", data={"message": "fresh", "conversation_id": ""}, headers=_HTMX)

    listed = await repository.conversations.list_recent(user.id, 10)
    [new] = [c for c in listed if c.id != earlier.id]
    sent = [m for m in repository.all_of(user.id) if m.conversation_id == new.id]
    assert [m.text for m in sent if m.role is ChatRole.USER] == ["fresh"]
    assert all(m.conversation_id != earlier.id for m in sent)


async def test_another_users_conversation_today_does_not_open_at_root() -> None:
    client, user, repository = _client()
    other = uuid4()
    theirs = await repository.start(other, "theirs")
    await repository.add(other, theirs, ChatRole.USER, "their secret", FROZEN_NOW)

    text = client.get("/").text

    assert "their secret" not in text
    assert "data-chat-chips" in text
    assert await repository.conversations.list_recent(user.id, 10) == []


def test_new_conversation_with_hx_request_returns_the_chat_partial() -> None:
    client, _, _ = _client()

    response = client.get("/c/new", headers=_HTMX)

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="chat-content"' in response.text


async def test_opening_new_conversation_and_leaving_stores_nothing() -> None:
    client, user, repository = _client()

    client.get("/c/new")
    client.get("/c/new", headers=_HTMX)
    client.get("/")

    assert await repository.conversations.list_recent(user.id, 10) == []
    assert repository.rows == []


def test_the_chat_page_offers_a_new_conversation_link_in_the_header_and_the_sidebar() -> None:
    client, _, _ = _client()

    text = client.get("/").text

    assert text.count('href="/c/new"') >= 2
    assert 'aria-label="New conversation"' in text


async def test_the_first_message_titles_the_conversation_and_moves_the_url_to_it() -> None:
    client, user, repository = _client()
    first = "  Rode   40 km\n\nwith  a lot of climbing today " + "x" * 80

    response = client.post(
        "/messages", data={"message": first, "conversation_id": ""}, headers=_HTMX
    )

    [conversation] = await repository.conversations.list_recent(user.id, 10)
    assert conversation.title == "Rode 40 km with a lot of climbing today " + "x" * 20
    assert len(conversation.title or "") == 60
    assert response.headers["HX-Replace-Url"] == f"/c/{conversation.id}"


async def test_a_message_to_an_existing_conversation_leaves_the_url_alone() -> None:
    client, user, repository = _client()
    thread = await repository.start(user.id)

    response = client.post(
        "/messages", data={"message": "more", "conversation_id": str(thread)}, headers=_HTMX
    )

    assert "HX-Replace-Url" not in response.headers


async def test_a_refused_first_message_starts_no_conversation_and_keeps_the_url() -> None:
    client, user, repository = _client()

    response = client.post("/messages", data={"message": " ", "conversation_id": ""}, headers=_HTMX)

    assert response.status_code == 422
    assert "HX-Replace-Url" not in response.headers
    assert await repository.conversations.list_recent(user.id, 10) == []


async def test_root_then_opens_the_new_conversation_and_the_old_one_stays_at_its_address() -> None:
    client, user, repository = _client()
    old = (await repository.conversations.create(user.id, "old", FROZEN_NOW - timedelta(days=1))).id
    await repository.add(user.id, old, ChatRole.USER, "in old", FROZEN_NOW - timedelta(days=1))

    client.post(
        "/messages", data={"message": "And 2 more of those", "conversation_id": ""}, headers=_HTMX
    )

    root = client.get("/").text
    assert "And 2 more of those" in root
    assert "in old" not in root
    assert "in old" in client.get(f"/c/{old}").text


def test_the_composer_ignores_a_second_send_while_the_first_is_in_flight() -> None:
    """A double-tapped first send posts twice with no conversation yet; htmx drops the second
    request while the first is pending, so only one conversation starts."""
    client, _, _ = _client()

    text = client.get("/c/new").text

    assert 'hx-sync="this:drop"' in text


# Retention (PRD-0004 B26, ADR-0021, ticket #148).

_EXPIRED = timedelta(days=14, seconds=1)


async def test_a_conversation_exactly_fourteen_days_old_still_opens() -> None:
    client, user, repository = _client()
    shown = await repository.conversations.create(user.id, "kept", FROZEN_NOW - timedelta(days=14))

    assert client.get(f"/c/{shown.id}").status_code == 200


async def test_an_expired_conversation_is_not_found_and_root_skips_it() -> None:
    client, user, repository = _client()
    expired = await repository.conversations.create(user.id, "old", FROZEN_NOW - _EXPIRED)

    response = client.get(f"/c/{expired.id}")
    root = client.get("/")

    assert response.status_code == 404
    assert _hidden_conversation("") in root.text


async def test_sending_to_an_expired_conversation_is_not_found() -> None:
    client, user, repository = _client()
    expired = await repository.conversations.create(user.id, "old", FROZEN_NOW - _EXPIRED)

    response = client.post(
        "/messages",
        data={"message": "hi", "conversation_id": str(expired.id)},
        headers=_HTMX,
    )

    assert response.status_code == 404
    assert repository.rows == []
