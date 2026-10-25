"""The chat thread in Liquid Glass v4 (ADR-0019 roll-out, ticket #266; Components Chat v4,
Navigation v4 and Loading v4): the athlete's bubble is `mat-bubble`, the reply avatar an
`lg-mark`, the composer an `lgs` pill with a tinted Send and a clear Stop, the starter chips
clear `lg-sm` controls, the suggestions popover thick `lgs`, Retry a clear `lg-xs`, and the
thinking indicator the glass ball whose word cycles until the server's phase replaces it. No
v1 `glass-*` or daisyUI `btn-*` class is left on any of them."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from ai_trainer.web.templating import build_templates
from tests.unit.test_chat_cards_liquid_glass import _Element, _Elements, _has_44px_tap_target, _one

_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "web"
_TEMPLATES = _ROOT / "templates"
_THREAD_TEMPLATES = (
    "partials/chat/message.html",
    "partials/chat/reply.html",
    "partials/chat/reply_failed.html",
    "partials/chat/reply_stopped.html",
    "partials/chat/reply_phase.html",
    "partials/chat/composer.html",
    "partials/chat/starter_chips.html",
    "partials/chat/suggestions.html",
    "partials/chat/action.html",
    "partials/chat/retry.html",
    "components/avatar.html",
    "components/thinking.html",
)
_V1_CLASS = re.compile(r"^(glass-.+|btn|btn-.+)$")
_MESSAGE_ID = UUID("00000000-0000-4000-8000-000000000266")


_COMPOSER = {"conversation_id": None, "error": None, "max_length": 4000}


def _render(template: str, **context: object) -> str:
    env = build_templates().env
    env.globals["reply_glyph"] = lambda sports: "app-mark"
    env.globals["failed_draft_sport"] = lambda message: None
    return env.get_template(template).render(**context)


def _message(role: str, text: str = "Sent Hades 8a") -> SimpleNamespace:
    return SimpleNamespace(id=_MESSAGE_ID, role=role, text=text, sports=[])


def _elements(html: str) -> list[_Element]:
    return _Elements(html).elements


@pytest.mark.parametrize("template", _THREAD_TEMPLATES)
def test_no_thread_template_keeps_a_v1_glass_or_daisyui_button_class(template: str) -> None:
    source = (_TEMPLATES / template).read_text()

    for attribute in re.findall(r'class="([^"]*)"', source):
        names = set(re.sub(r"\{[{%].*?[%}]\}", " ", attribute).split())
        assert not {name for name in names if _V1_CLASS.match(name)}, (template, attribute)


def test_the_athletes_message_is_a_mat_bubble() -> None:
    html = _render(
        "partials/chat/message.html", message=_message("user"), draft_cards={}, choice_cards={}
    )

    (bubble,) = [e for e in _elements(html) if "chat-message" in e.classes]
    assert {"mat-bubble", "rounded-3xl", "rounded-br-lg"} <= bubble.classes


def test_the_reply_avatar_is_a_clear_lg_mark() -> None:
    html = _render(
        "partials/chat/message.html", message=_message("assistant"), draft_cards={}, choice_cards={}
    )

    avatar = _one(_elements(html), **{"data-avatar": "app-mark"})
    assert {"lg", "lg-clear", "lg-mark", "text-primary"} <= avatar.classes


def test_the_composer_is_an_lgs_pill_with_a_tinted_send_disabled_while_empty() -> None:
    html = _render("partials/chat/composer.html", text="", **_COMPOSER)

    elements = _elements(html)
    (form,) = [e for e in elements if "data-chat-form" in e.attrs]
    send = _one(elements, **{"aria-label": "Send"})
    assert {"lgs", "relative", "rounded-[26px]"} <= form.classes
    assert {"lg", "lg-tinted", "lg-icon"} <= send.classes
    assert "disabled" in send.attrs


def test_send_is_enabled_when_the_composer_comes_back_with_text() -> None:
    html = _render("partials/chat/composer.html", text="Board session", **_COMPOSER)

    assert "disabled" not in _one(_elements(html), **{"aria-label": "Send"}).attrs


def test_send_turns_into_a_clear_stop_while_a_reply_streams() -> None:
    html = _render("partials/chat/composer.html", text="", streaming_id=_MESSAGE_ID, **_COMPOSER)

    elements = _elements(html)
    stop = _one(elements, **{"aria-label": "Stop generating"})
    assert {"lg", "lg-clear", "lg-icon"} <= stop.classes
    assert stop.attrs["hx-get"] == f"/messages/{_MESSAGE_ID}/stop"
    assert not [e for e in elements if e.attrs.get("aria-label") == "Send"]


def test_starter_chips_are_clear_small_lg_controls_with_a_44px_tap_target() -> None:
    elements = _elements(_render("partials/chat/starter_chips.html"))

    chips = [e for e in elements if "data-chat-chip" in e.attrs]
    assert len(chips) == 4
    for chip in chips:
        assert {"lg", "lg-clear", "lg-sm"} <= chip.classes
        assert _has_44px_tap_target(elements, chip, inset_y="-inset-y-1")


def test_the_suggestions_popover_is_thick_floating_glass_that_keeps_its_listbox() -> None:
    elements = _elements(_render("partials/chat/suggestions.html"))

    popover = _one(elements, id="chat-suggestions")
    assert {"lgs", "lgs-thick", "relative", "rounded-[22px]"} <= popover.classes
    assert popover.attrs["role"] == "listbox"
    options = [e for e in elements if e.attrs.get("role") == "option"]
    assert options
    assert all(e.attrs["aria-selected"] == "false" for e in options)


def test_the_suggestions_popover_shows_its_keyboard_hint_on_desktop_only() -> None:
    html = _render("partials/chat/suggestions.html")

    (hint,) = [e for e in _elements(html) if "data-suggestion-keys" in e.attrs]
    assert {"hidden", "lg:flex"} <= hint.classes
    # Inside role=listbox only options belong to the accessibility tree; the combobox already
    # tells assistive tech how the keys work.
    assert hint.attrs.get("aria-hidden") == "true"
    assert "accept" in html
    assert "close" in html


@pytest.mark.parametrize(
    ("template", "context"),
    [
        (
            "partials/chat/reply_failed.html",
            {"message_id": _MESSAGE_ID, "limited": False, "timed_out": True},
        ),
        ("partials/chat/reply_stopped.html", {"message_id": _MESSAGE_ID}),
    ],
)
def test_retry_is_a_clear_extra_small_lg_with_a_44px_tap_target(
    template: str, context: dict[str, object]
) -> None:
    elements = _elements(_render(template, **context))

    retry = _one(elements, **{"hx-get": f"/messages/{_MESSAGE_ID}/retry"})
    assert {"lg", "lg-clear", "lg-xs"} <= retry.classes
    assert _has_44px_tap_target(elements, retry, inset_y="-inset-y-2")


def _thinking(html: str) -> tuple[list[_Element], _Element]:
    elements = _elements(html)
    (indicator,) = [e for e in elements if "data-thinking" in e.attrs]
    return elements, indicator


def test_the_thinking_indicator_is_the_glass_ball() -> None:
    _, indicator = _thinking(_render("partials/chat/reply.html", message=_message("user")))

    (ball,) = [e for e in indicator.descendants if "ld-ball" in e.classes]
    assert ball.attrs.get("aria-hidden") == "true"
    parts = {name for e in ball.descendants for name in e.classes if name.startswith("ld-ball-")}
    assert parts == {
        "ld-ball-floor", "ld-ball-sphere", "ld-ball-caustic", "ld-ball-orbit", "ld-ball-ox",
        "ld-ball-oy", "ld-ball-photon", "ld-ball-spec", "ld-ball-hot",
    }  # fmt: skip


def test_the_thinking_word_cycles_through_words_the_template_holds() -> None:
    elements, _ = _thinking(_render("partials/chat/reply.html", message=_message("user")))

    phase = _one(elements, id=f"reply-{_MESSAGE_ID}-phase")
    words = json.loads(phase.attrs["data-thinking-words"] or "[]")
    (word,) = [e for e in phase.descendants if "data-thinking-word" in e.attrs]
    assert len(words) > 1
    assert all(w.endswith("…") for w in words)
    assert word.attrs.get("aria-hidden") == "true"
    assert any("sr-only" in e.classes for e in phase.descendants)


def test_an_sse_phase_replaces_the_cycling_word() -> None:
    html = _render("partials/chat/reply_phase.html", message_id=_MESSAGE_ID, phase="routing")

    (template,) = [e for e in _elements(html) if e.tag == "template"]
    assert template.attrs["hx-target"] == f"#reply-{_MESSAGE_ID}-phase"
    assert template.attrs["hx-swap"] == "innerHTML"
    assert "Reading your message…" in html
    assert "data-thinking-word" not in html


def test_the_still_working_hint_waits_for_the_script() -> None:
    _, indicator = _thinking(_render("partials/chat/reply.html", message=_message("user")))

    (hint,) = [e for e in indicator.descendants if "data-thinking-hint" in e.attrs]
    assert "hidden" in hint.attrs
    assert "HINT_AFTER_MS = 8000" in (_ROOT / "static" / "js" / "thinking.js").read_text()


def _utility(css: str, name: str) -> str:
    match = re.search(rf"@utility {re.escape(name)} \{{(.*?)\n\}}", css, re.S)
    assert match, name
    return match.group(1)


def test_the_glass_ball_and_thick_surface_are_harvested_as_utilities() -> None:
    css = (_ROOT / "static" / "css" / "glass.css").read_text()

    assert "--lgs-tint" in _utility(css, "lgs-thick")
    for name in ("ld-ball", "ld-ball-sphere", "ld-ball-photon", "ld-ball-spec", "ld-ball-hot"):
        _utility(css, name)
    assert "@keyframes ld-ball-x" in css
    assert "@keyframes ld-ball-y" in css


def test_under_reduced_motion_the_ball_stands_still_and_only_the_word_changes() -> None:
    css = (_ROOT / "static" / "css" / "glass.css").read_text()
    script = (_ROOT / "static" / "js" / "thinking.js").read_text()

    for name in ("ld-ball-ox", "ld-ball-oy"):
        body = _utility(css, name)
        reduced = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\}", body, re.S)
        assert reduced, name
        assert "animation: none" in reduced.group(1), name
    assert "prefers-reduced-motion: reduce" in script


def test_tab_accepts_a_suggestion_as_the_popovers_hint_says() -> None:
    script = (_ROOT / "static" / "js" / "autocomplete.js").read_text()

    assert 'event.key === "Tab"' in script


def test_a_hairline_separates_a_suggestion_group_from_a_shown_group_above_it() -> None:
    groups = [
        e
        for e in _elements(_render("partials/chat/suggestions.html"))
        if "data-suggestion-group" in e.attrs
    ]

    shown_before = "[[data-suggestion-group]:not([hidden])~&]"
    assert len(groups) == 2
    for group in groups:
        assert {f"{shown_before}:border-t", f"{shown_before}:border-base-content/10"} <= (
            group.classes
        )
