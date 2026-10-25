"""Composer suggestions on the chat (PRD-0003 F13, ADR-0019, ticket #85): starter chips on an
empty chat, and the suggestion listbox the composer filters while the athlete types. Filtering,
highlighting and the keyboard live in `static/js/autocomplete.js`; these tests pin the markup
it works on."""

import re
from html.parser import HTMLParser
from uuid import uuid4

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.templating import STATIC_DIR, build_templates
from tests.chat_support import FROZEN_NOW, InMemoryChatRepository, include_chat, sign_in_as

_HTMX = {"HX-Request": "true"}
_MAX_SHOWN = 6


class _Elements(HTMLParser):
    """Every start tag with its attributes, in document order."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def having(self, attribute: str) -> list[tuple[str, dict[str, str | None]]]:
        return [(tag, attrs) for tag, attrs in self.tags if attribute in attrs]


def _user() -> User:
    return User(
        id=uuid4(),
        sub=f"sub-{uuid4()}",
        email="athlete@example.com",
        name="Ada Lovelace",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
        timezone="UTC",
    )


def _client() -> tuple[TestClient, User, InMemoryChatRepository]:
    app = FastAPI()
    repository = include_chat(app, build_templates())
    user = sign_in_as(app, _user())
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return TestClient(app), user, repository


def test_empty_chat_offers_three_or_four_starter_chips_that_do_not_submit() -> None:
    client, _, _ = _client()

    chips = _Elements(client.get("/").text).having("data-chat-chip")

    assert 3 <= len(chips) <= 4
    for tag, attrs in chips:
        assert tag == "button"
        assert attrs["type"] == "button"
        assert attrs["data-chat-chip"]


def test_starter_chips_keep_a_44px_tap_target() -> None:
    client, _, _ = _client()

    page = client.get("/").text
    chips = re.findall(r"<button[^>]*data-chat-chip[^>]*>(.*?)</button>", page, re.S)

    # 36 px lg-sm chips grow by 4 px each way through an invisible child (#266): an lg's own
    # ::before and ::after are its rim and light.
    assert len(chips) == len(_Elements(page).having("data-chat-chip")) > 0
    for chip in chips:
        assert chip.startswith('<span class="absolute -inset-x-px -inset-y-1" aria-hidden="true">')


async def test_chips_are_not_rendered_once_the_chat_has_history() -> None:
    client, user, repository = _client()
    assert _Elements(client.get("/").text).having("data-chat-chip")
    await repository.add(
        user.id, await repository.conversation_of(user.id), ChatRole.USER, "hello", FROZEN_NOW
    )

    assert _Elements(client.get("/").text).having("data-chat-chip") == []


def test_the_chip_row_hides_itself_once_a_message_is_appended() -> None:
    client, _, _ = _client()

    rows = [attrs for _, attrs in _Elements(client.get("/").text).having("data-chat-chips")]

    assert len(rows) == 1
    assert "group-has-[.chat-message]/chat:hidden" in (rows[0]["class"] or "")


def test_the_chip_row_hides_while_the_composer_has_text() -> None:
    client, _, _ = _client()

    [row] = [attrs for _, attrs in _Elements(client.get("/").text).having("data-chat-chips")]

    assert "group-has-[[data-chat-input]:not(:placeholder-shown)]/chat:hidden" in (
        row["class"] or ""
    )


def _listbox(html: str) -> tuple[dict[str, str | None], list[dict[str, str | None]]]:
    elements = _Elements(html)
    [listbox] = [attrs for tag, attrs in elements.tags if attrs.get("id") == "chat-suggestions"]
    options = [attrs for _, attrs in elements.tags if attrs.get("role") == "option"]
    return listbox, options


def test_composer_carries_the_suggestion_listbox_hidden() -> None:
    client, _, _ = _client()

    listbox, options = _listbox(client.get("/").text)

    assert listbox["role"] == "listbox"
    assert "hidden" in listbox
    assert len(options) > _MAX_SHOWN
    for option in options:
        assert option["id"]
        assert option["data-suggestion"]
        assert option["aria-selected"] == "false"


def test_several_suggestions_start_with_how() -> None:
    client, _, _ = _client()

    _, options = _listbox(client.get("/").text)
    texts = [option["data-suggestion"] or "" for option in options]

    assert len([text for text in texts if "how" in text.lower()]) >= 2


def test_the_textarea_is_an_aria_combobox_for_the_suggestions() -> None:
    client, _, _ = _client()

    [(_, textarea)] = _Elements(client.get("/").text).having("data-chat-input")

    assert textarea["role"] == "combobox"
    assert textarea["aria-autocomplete"] == "list"
    assert textarea["aria-controls"] == "chat-suggestions"
    assert textarea["aria-expanded"] == "false"


def test_the_composer_after_a_send_still_carries_the_suggestions() -> None:
    client, _, _ = _client()

    response = client.post("/messages", data={"message": "hello"}, headers=_HTMX)

    listbox, options = _listbox(response.text)
    assert listbox["role"] == "listbox"
    assert options


def test_chat_page_loads_the_autocomplete_script() -> None:
    client, _, _ = _client()

    assert '<script src="/static/js/autocomplete.js" defer></script>' in client.get("/").text
    assert client.get("/static/js/autocomplete.js").status_code == 200
