"""The chat cards in Liquid Glass v4 (ADR-0019 roll-out, ticket #265, the Components Chat v4
mock): the draft card, its edit form and end-state rows, and the choice card are content
(`mat-content`) with `lg` controls on them, never glass on glass, and no v1 `glass-*` or
daisyUI `btn-*` class is left on any of them."""

import re
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    ScriptedFastRouter,
    include_chat,
    pending_draft,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_TEMPLATES = Path(__file__).parents[2] / "src" / "ai_trainer" / "web" / "templates"
_CARD_TEMPLATES = (
    "partials/chat/draft_card.html",
    "partials/chat/draft_edit.html",
    "partials/chat/edit_climbing.html",
    "partials/chat/edit_gym.html",
    "partials/chat/edit_cycling.html",
    "partials/chat/draft_climbing.html",
    "partials/chat/draft_gym.html",
    "partials/chat/draft_cycling.html",
    "partials/chat/draft_confirmed.html",
    "partials/chat/draft_discarded.html",
    "partials/chat/draft_removed.html",
    "partials/chat/draft_failed.html",
    "components/choice_card.html",
)
# daisyUI component classes the v4 cards replace with `lg-*` controls and `lg-field`.
_V1_CLASS = re.compile(r"^(btn|btn-.+|badge|badge-.+|input|input-.+|select|select-.+)$")
# The draft card's sport menu floats over the card: functional glass, not a control on it.
_FLOATING_MENU = "dropdown-content"


class _Element:
    def __init__(self, tag: str, classes: set[str], attrs: dict[str, str | None]) -> None:
        self.tag = tag
        self.classes = classes
        self.attrs = attrs
        self.ancestors: list[set[str]] = []
        self.parent: _Element | None = None
        self.descendants: list[_Element] = []


class _Elements(HTMLParser):
    """Every element of a fragment with its classes and its ancestors' classes."""

    _VOID = frozenset({"input", "br", "img", "hr", "meta", "link"})

    def __init__(self, html: str) -> None:
        super().__init__()
        self.elements: list[_Element] = []
        self._stack: list[_Element] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag, set((dict(attrs).get("class") or "").split()), dict(attrs))
        element.ancestors = [parent.classes for parent in self._stack]
        element.parent = self._stack[-1] if self._stack else None
        for parent in self._stack:
            parent.descendants.append(element)
        self.elements.append(element)
        if tag not in self._VOID:
            self._stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        while self._stack:
            if self._stack.pop().tag == tag:
                break


def _is_glass(classes: set[str]) -> bool:
    return any(name.startswith("glass-") for name in classes)


def _assert_v4(html: str) -> list[_Element]:
    elements = _Elements(html).elements
    _assert_v4_elements(elements)
    return elements


def _assert_v4_elements(elements: list[_Element]) -> None:
    """No v1 control class, no glass surface but the floating sport menu, and no `lg` on a
    `glass-*` surface or inside another `lg`."""
    for element in elements:
        v1 = {name for name in element.classes if _V1_CLASS.match(name)}
        assert not v1, f"v1 classes {v1} on <{element.tag} class={element.classes}>"
        if _is_glass(element.classes):
            assert _FLOATING_MENU in element.classes, f"glass on <{element.tag}>"
        if "lg" in element.classes:
            assert not any("lg" in up or _is_glass(up) for up in element.ancestors), (
                f"glass on glass: <{element.tag} class={element.classes}>"
            )


def _one(elements: list[_Element], **attrs: str) -> _Element:
    (found,) = [e for e in elements if all(e.attrs.get(k) == v for k, v in attrs.items())]
    return found


def _has_44px_tap_target(
    elements: list[_Element], control: _Element, inset_y: str = "-inset-y-[6px]"
) -> bool:
    """A control under 44 px grows its hit area with an invisible child past its top and
    bottom edges (6 px for a 32 px chip); its own `::before` and `::after` are the `lg` rim and
    light, so the layer can't be one of them. The child sets its sides too: an empty absolute
    box with only `top` and `bottom` set is zero px wide."""
    return any(
        e.parent is control
        and {"absolute", inset_y, "-inset-x-px"} <= e.classes
        and e.attrs.get("aria-hidden") == "true"
        for e in elements
    )


def _with_text(html: str, tag: str, text: str) -> _Element:
    (markup,) = re.findall(
        rf"<{tag}\b[^>]*>(?:(?!</{tag}>).)*{text}(?:(?!</{tag}>).)*</{tag}>", html, re.S
    )
    return _Elements(markup).elements[0]


class _App:
    def __init__(self) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.profile = InMemoryProfile()
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=ScriptedConversation(
                intents=[[Intent(kind="log_session", sport="climbing")]]
            ),
            profile=self.profile,
            drafts=self.drafts,
            clarifications=self.clarifications,
            fast_router=ScriptedFastRouter(
                intents=None, plausible_sports=("gym", "cycling"), single_intent="log_session"
            ),
        )
        self.user = sign_in_as(app)
        self.client = TestClient(app)

    async def draft_id(self) -> UUID:
        return (await pending_draft(self.drafts, self.user.id)).id


@pytest.mark.parametrize("template", _CARD_TEMPLATES)
def test_no_card_template_keeps_a_v1_glass_or_daisyui_control_class(template: str) -> None:
    source = (_TEMPLATES / template).read_text()

    for attribute in re.findall(r'class="([^"]*)"', source):
        names = set(re.sub(r"\{[{%].*?[%}]\}", " ", attribute).split())
        assert not {name for name in names if _V1_CLASS.match(name)}, (template, attribute)
        if _is_glass(names):
            assert _FLOATING_MENU in names, (template, attribute)


async def test_the_draft_card_is_content_with_tinted_confirm_clear_edit_and_quiet_discard() -> None:
    app = _App()
    draft_id = await app.draft_id()

    html = app.client.get(f"/drafts/{draft_id}", headers=_HTMX).text

    elements = _assert_v4(html)
    assert "mat-content" in _one(elements, **{"data-draft-id": str(draft_id)}).classes
    confirm = _one(elements, **{"hx-post": f"/drafts/{draft_id}/confirm"})
    discard = _one(elements, **{"hx-post": f"/drafts/{draft_id}/discard"})
    assert {"lg", "lg-tinted"} <= confirm.classes
    assert {"lg", "lg-quiet"} <= discard.classes
    assert {"lg", "lg-clear"} <= _with_text(html, "button", "Edit</button>").classes


async def test_the_date_chip_is_a_clear_lg_chip_that_keeps_a_44px_tap_target() -> None:
    app = _App()
    draft_id = await app.draft_id()

    html = app.client.get(f"/drafts/{draft_id}", headers=_HTMX).text

    elements = _assert_v4(html)
    chip = _one(elements, **{"data-date-source": "assumed_today"})
    assert {"lg", "lg-clear", "lg-chip"} <= chip.classes
    assert _has_44px_tap_target(elements, chip)


async def test_the_sport_chip_is_a_clear_lg_chip_that_keeps_a_44px_tap_target() -> None:
    app = _App()
    app.profile.sports[app.user.id] = ["climbing", "gym"]
    draft_id = await app.draft_id()

    html = app.client.get(f"/drafts/{draft_id}", headers=_HTMX).text

    elements = _assert_v4(html)
    (chip,) = [e for e in elements if e.tag == "summary"]
    assert {"lg", "lg-clear", "lg-chip"} <= chip.classes
    assert _has_44px_tap_target(elements, chip)


async def test_the_edit_form_is_content_with_lg_fields_and_lg_actions() -> None:
    app = _App()
    draft_id = await app.draft_id()

    html = app.client.get(f"/drafts/{draft_id}/edit", headers=_HTMX).text

    elements = _assert_v4(html)
    assert "mat-content" in elements[0].classes
    fields = [
        e for e in elements if e.tag in {"input", "select"} and e.attrs.get("type") != "hidden"
    ]
    assert fields
    assert all("lg-field" in field.classes for field in fields)
    assert all("lg-select" in field.classes for field in fields if field.tag == "select")
    assert {"lg", "lg-tinted"} <= _one(elements, type="submit").classes
    assert {"lg", "lg-clear"} <= _one(elements, **{"hx-get": f"/drafts/{draft_id}"}).classes


async def test_a_rejected_edit_value_shows_its_error_under_an_invalid_lg_field() -> None:
    app = _App()
    draft_id = await app.draft_id()

    response = app.client.post(
        f"/drafts/{draft_id}/edit",
        data={"occurred_on": "2026-10-02", "rpe": "11"},
        headers=_HTMX,
    )

    assert response.status_code == 422
    elements = _assert_v4(response.text)
    rpe = _one(elements, name="rpe")
    assert "lg-field" in rpe.classes
    assert rpe.attrs["aria-invalid"] == "true"
    hint = _one(elements, id=rpe.attrs["aria-describedby"] or "")
    assert "text-error" in hint.classes


async def test_a_confirmed_draft_is_a_content_row_with_a_ghost_undo() -> None:
    app = _App()
    draft_id = await app.draft_id()

    html = app.client.post(f"/drafts/{draft_id}/confirm", headers=_HTMX).text

    elements = _assert_v4(html)
    assert "mat-content" in _one(elements, **{"data-confirmed-draft": str(draft_id)}).classes
    undo = _one(elements, **{"hx-post": f"/drafts/{draft_id}/undo"})
    assert {"lg", "lg-ghost", "lg-sm"} <= undo.classes
    # 36 px tall, so the invisible child reaches 4 px past its top and bottom.
    assert _has_44px_tap_target(elements, undo, "-inset-y-1")


async def test_discarded_and_removed_drafts_are_dashed_rows_without_a_material() -> None:
    app = _App()
    discarded, removed = await app.draft_id(), await app.draft_id()
    app.client.post(f"/drafts/{removed}/confirm", headers=_HTMX)

    rows = (
        (
            app.client.post(f"/drafts/{discarded}/discard", headers=_HTMX).text,
            "data-discarded-draft",
        ),
        (app.client.post(f"/drafts/{removed}/undo", headers=_HTMX).text, "data-removed-draft"),
    )

    for html, marker in rows:
        (row,) = [e for e in _assert_v4(html) if marker in e.attrs]
        assert {"border", "border-dashed", "min-h-[62px]"} <= row.classes
        assert "mat-content" not in row.classes


async def _question(app: _App) -> tuple[UUID, str]:
    app.profile.sports[app.user.id] = ["gym", "cycling"]
    message = await app.chat.add(
        app.user.id,
        await app.chat.conversation_of(app.user.id),
        ChatRole.USER,
        "Did intervals",
        FROZEN_NOW,
    )
    stream = app.client.get(f"/messages/{message.id}/reply").text
    [question] = app.clarifications.all_of(app.user.id)
    return question.id, stream


async def test_the_choice_card_is_content_with_clear_lg_rows_and_a_ghost_type_instead() -> None:
    app = _App()
    question_id, stream = await _question(app)

    (card_html,) = re.findall(
        rf'<div[^>]*id="choice-{question_id}".*?Let me type instead</button>', stream, re.S
    )
    elements = _assert_v4(card_html)
    assert "mat-content" in elements[0].classes
    rows = [e for e in elements if "data-choice-option" in e.attrs]
    assert len(rows) == 2
    assert all({"lg", "lg-clear", "lg-row"} <= row.classes for row in rows)
    skip = _one(elements, **{"hx-post": f"/clarifications/{question_id}/abandon"})
    assert {"lg", "lg-ghost"} <= skip.classes


async def test_an_answered_choice_is_a_content_row_and_a_skipped_one_is_dashed() -> None:
    answered_app, skipped_app = _App(), _App()
    answered_id, _ = await _question(answered_app)
    skipped_id, _ = await _question(skipped_app)

    answered = answered_app.client.post(
        f"/clarifications/{answered_id}/answer", data={"option_ids": ["gym"]}, headers=_HTMX
    ).text
    skipped = skipped_app.client.post(f"/clarifications/{skipped_id}/abandon", headers=_HTMX).text

    # The answer swaps in the resumed reply too; its messages are the chat thread's (#266).
    answered_row = _one(
        _Elements(answered).elements, **{"data-choice-card": str(answered_id), "role": "status"}
    )
    _assert_v4_elements([answered_row, *answered_row.descendants])
    skipped_row = _one(_assert_v4(skipped), **{"data-choice-card": str(skipped_id)})
    assert "mat-content" in answered_row.classes
    assert {"border", "border-dashed", "min-h-[62px]"} <= skipped_row.classes
    assert "mat-content" not in skipped_row.classes


def test_glass_css_harvests_the_quiet_variant_and_the_xs_size() -> None:
    glass_css = (_TEMPLATES.parent / "static" / "css" / "glass.css").read_text()

    for utility in ("lg-quiet", "lg-xs"):
        assert f"@utility {utility} {{" in glass_css
    assert ".chip-cal" in glass_css
    assert ".chip-edit" in glass_css


def test_a_failed_drafts_retry_is_a_clear_xs_control_with_a_44px_tap_target() -> None:
    source = (_TEMPLATES / "partials/chat/draft_failed.html").read_text()

    (retry,) = re.findall(r"<button[^>]*retry-draft[^>]*>.*?</button>", source, re.S)
    elements = _Elements(retry).elements
    assert {"lg", "lg-clear", "lg-xs"} <= elements[0].classes
    # 28 px tall, so the invisible child reaches 8 px past every edge.
    assert any(e.parent is elements[0] and {"absolute", "-inset-2"} <= e.classes for e in elements)
