"""`GET /` and `POST /messages` (PRD-0003 F1, B13, ADR-0008, ticket #78): the chat page, and a
message appended to it through htmx."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

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
    await repository.add(user.id, ChatRole.USER, "first one", FROZEN_NOW)
    await repository.add(user.id, ChatRole.USER, "second one", FROZEN_NOW + timedelta(minutes=1))

    text = client.get("/").text

    assert text.index("first one") < text.index("second one")


async def test_history_shows_only_the_latest_fifty() -> None:
    client, user, repository = _client()
    for index in range(55):
        await repository.add(
            user.id,
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
    await repository.add(user_a.id, ChatRole.USER, "for a only", FROZEN_NOW)

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
