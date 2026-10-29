"""The settings, admin, sign-in, status and error pages in Liquid Glass v4 (ADR-0019 roll-out,
ticket #268, the Components Core v4 mock): cards are `mat-content`, controls are `lg` variants
(`lg-danger` for delete, `lg-ghost` for cancel), fields are `lg-field` / `lg-select`, the theme
toggle is the `lgs` segmented control, and the dialogs and the admin toast float as
`lgs lgs-thick`. No v1 `glass-*` or daisyUI `btn` class is left on any of them."""

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.templating import STATIC_DIR, build_templates

_TEMPLATES = Path(__file__).parents[2] / "src" / "ai_trainer" / "web" / "templates"
_SCOPED_FEATURES = ("settings", "admin", "auth", "errors")
_SCOPED_TEMPLATES = (
    *(
        str(path.relative_to(_TEMPLATES))
        for kind in ("pages", "partials")
        for feature in _SCOPED_FEATURES
        for path in sorted((_TEMPLATES / kind / feature).glob("*.html"))
    ),
    "components/status_card.html",
    "components/drop_sport_dialog.html",
)
_V1_CLASS = re.compile(r"^(glass-.+|btn|btn-.+)$")
_NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class _Element:
    def __init__(self, tag: str, attrs: dict[str, str | None]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.classes = set((attrs.get("class") or "").split())
        self.descendants: list[_Element] = []


class _Elements(HTMLParser):
    """Every element of a fragment with its classes and its descendants."""

    _VOID = frozenset(
        {"input", "br", "img", "hr", "meta", "link", "path", "circle", "line", "rect", "polyline"}
    )

    def __init__(self, html: str) -> None:
        super().__init__()
        self.elements: list[_Element] = []
        self._stack: list[_Element] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag, dict(attrs))
        for parent in self._stack:
            parent.descendants.append(element)
        self.elements.append(element)
        if tag not in self._VOID:
            self._stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def with_class(self, *names: str) -> list[_Element]:
        return [e for e in self.elements if set(names) <= e.classes]

    def one(self, predicate: str) -> _Element:
        [element] = [e for e in self.elements if predicate in e.attrs]
        return element


def _render(template: str, **context: object) -> str:
    return build_templates().env.get_template(template).render(**context)


def _user(email: str, status: UserStatus, name: str | None = None) -> User:
    return User(
        id=uuid4(),
        sub=email,
        email=email,
        name=name,
        locale="en",
        status=status,
        created_at=_NOW,
    )


def _admin_users_html() -> tuple[str, User, User]:
    admin = _user("admin@example.com", UserStatus.ACTIVE, "Ada Admin")
    other = _user("athlete@example.com", UserStatus.PENDING)
    html = _render(
        "partials/admin/users.html",
        users=[admin, other],
        current_user=admin,
        pending_count=1,
        total_count=2,
    )
    return html, admin, other


# AC1: no `glass-*` or `btn-*` class is left in the scoped templates.


@pytest.mark.parametrize("template", _SCOPED_TEMPLATES)
def test_no_v1_glass_or_btn_class_is_left(template: str) -> None:
    source = (_TEMPLATES / template).read_text()

    classes = {
        token
        for value in re.findall(r'class="([^"]*)"', source)
        for token in re.sub(r"\{[{%].*?[%}]\}", " ", value).split()
    }
    assert not {c for c in classes if _V1_CLASS.match(c)}, template


def _rendered_pages() -> list[tuple[str, str]]:
    admin = _user("admin@example.com", UserStatus.ACTIVE, "Ada Admin")
    other = _user("athlete@example.com", UserStatus.PENDING)
    users = {"users": [admin, other], "current_user": admin, "pending_count": 1, "total_count": 2}
    update = {"user": other, "current_user": admin, "pending_count": 0}
    sport = SimpleNamespace(id="climbing", label_key="sport.climbing.label", icon="mountain")
    module = build_templates().env.get_template("components/drop_sport_dialog.html").module
    dialog = module.drop_sport_dialog  # type: ignore[attr-defined]
    pages = [
        ("settings", _render("partials/settings/index.html")),
        ("admin", _render("partials/admin/users.html", **users)),
        (
            "admin update",
            _render("partials/admin/user_update.html", **update, previous_status=UserStatus.ACTIVE),
        ),
        ("admin error", _render("partials/admin/status_change_error.html")),
        ("admin forbidden", _render("partials/admin/forbidden.html")),
        ("sign-in", _render("partials/auth/sign_in.html", sign_in_failed=True)),
        (
            "drop dialog",
            str(dialog([(sport, [SimpleNamespace(text="7a")])], "/x", "#y")),
        ),
    ]
    pages += [
        (s, _render("partials/auth/status.html", status=s, email="a@x.io"))
        for s in ("pending", "disabled")
    ]
    pages += [
        (c, _render(f"partials/errors/{c}.html")) for c in ("401", "403", "404", "429", "500")
    ]
    return pages


def test_no_v1_glass_or_btn_class_reaches_the_rendered_pages() -> None:
    """Catches a v1 class that arrives through a Jinja expression or a macro argument, which
    the source scan above strips out."""
    for name, html in _rendered_pages():
        v1 = {c for e in _Elements(html).elements for c in e.classes if _V1_CLASS.match(c)}
        assert not v1, (name, v1)


def test_status_card_variants_are_lg_not_btn() -> None:
    source = (_TEMPLATES / "components" / "status_card.html").read_text()

    assert "btn" not in source
    assert "glass-" not in source


# Settings: content cards, the theme segment, the danger zone and its dialog.


def test_settings_cards_are_the_content_material() -> None:
    elements = _Elements(_render("partials/settings/index.html"))

    cards = elements.with_class("mat-content")
    # Appearance and the danger zone, plus one card per Add to home screen state (#307), of
    # which `install.js` shows one.
    install = [card for card in cards if "data-install-state" in card.attrs]
    assert len(cards) - len(install) == 2
    assert len(install) == 9


def test_theme_toggle_is_the_lgs_segmented_control() -> None:
    elements = _Elements(_render("partials/settings/index.html"))

    [track] = [
        e for e in elements.elements if e.attrs.get("aria-label") == "Theme" and e.tag == "div"
    ]
    assert {"lgs", "rounded-full"} <= track.classes
    options = {
        e.attrs["data-theme-option"]: e for e in track.descendants if "data-theme-option" in e.attrs
    }
    assert {"lg", "lg-clear", "lg-seg-on"} <= options["trainer-dark"].classes
    assert options["trainer-dark"].attrs["aria-pressed"] == "true"
    assert options["trainer-light"].classes >= {"lg-seg-item"}
    assert "lg" not in options["trainer-light"].classes
    assert options["trainer-light"].attrs["aria-pressed"] == "false"


def test_theme_segments_grow_a_44px_tap_target() -> None:
    """The mock's segments are 40 px; an invisible child grows each tap target to 44 px."""
    elements = _Elements(_render("partials/settings/index.html"))

    for option in [e for e in elements.elements if "data-theme-option" in e.attrs]:
        assert "relative" in option.classes
        assert any(
            {"absolute", "-inset-y-0.5"} <= d.classes and d.attrs.get("aria-hidden") == "true"
            for d in option.descendants
        )


def test_theme_script_swaps_the_segment_classes() -> None:
    script = (STATIC_DIR / "js" / "theme.js").read_text()

    assert "lg-seg-on" in script
    assert "lg-seg-item" in script
    assert "tab-active" not in script


def test_delete_account_trigger_is_lg_danger() -> None:
    elements = _Elements(_render("partials/settings/index.html"))

    trigger = elements.one("data-delete-account-trigger")
    assert {"lg", "lg-danger"} <= trigger.classes


def test_delete_account_dialog_is_thick_floating_glass_with_an_lg_field() -> None:
    elements = _Elements(_render("partials/settings/index.html"))

    [box] = elements.with_class("modal-box")
    assert {"lgs", "lgs-thick"} <= box.classes
    field = elements.one("data-delete-account-input")
    assert "lg-field" in field.classes


def test_delete_account_confirm_is_lg_danger_and_starts_disabled() -> None:
    """AC2: Delete stays disabled until the athlete types DELETE (`delete_account.js`)."""
    elements = _Elements(_render("partials/settings/index.html"))

    confirm = elements.one("data-delete-account-confirm")
    assert {"lg", "lg-danger"} <= confirm.classes
    assert "disabled" in confirm.attrs


def test_delete_account_cancel_is_lg_ghost() -> None:
    elements = _Elements(_render("partials/settings/index.html"))

    [cancel] = [e for e in elements.elements if e.tag == "button" and "lg-ghost" in e.classes]
    assert "lg" in cancel.classes


# Status and error pages.


@pytest.mark.parametrize("status", ["pending", "disabled"])
def test_status_card_is_content_with_a_tinted_primary_and_ghost_secondary(status: str) -> None:
    elements = _Elements(_render("partials/auth/status.html", status=status, email="a@example.com"))

    assert elements.with_class("mat-content")
    [primary] = elements.with_class("lg", "lg-tinted")
    [secondary] = elements.with_class("lg", "lg-ghost")
    assert primary.attrs.get("href") == "/auth/login"
    assert secondary.attrs.get("hx-post") == "/auth/logout"


@pytest.mark.parametrize("code", ["401", "403", "404", "429", "500"])
def test_error_pages_are_content_cards_with_a_tinted_primary(code: str) -> None:
    elements = _Elements(_render(f"partials/errors/{code}.html"))

    assert elements.with_class("mat-content")
    assert len(elements.with_class("lg", "lg-tinted")) == 1


def test_a_lone_secondary_action_is_lg_clear() -> None:
    module = build_templates().env.get_template("components/status_card.html").module
    html = str(
        module.status_card(  # type: ignore[attr-defined]
            icon_name="route",
            kicker="k",
            title="t",
            body="b",
            secondary={"label": "Back", "href": "/"},
        )
    )
    elements = _Elements(html)

    [secondary] = elements.with_class("lg", "lg-clear")
    assert secondary.attrs.get("href") == "/"


# Sign-in.


def test_sign_in_surfaces_are_v4() -> None:
    elements = _Elements(_render("partials/auth/sign_in.html", sign_in_failed=True))

    assert elements.with_class("lg", "lg-clear", "lg-mark")
    assert len(elements.with_class("mat-content")) >= 3
    assert len(elements.with_class("mat-bubble")) == 2
    [alert] = [e for e in elements.elements if e.attrs.get("role") == "alert"]
    assert "mat-content" in alert.classes
    google = elements.one("data-google-sign-in")
    assert not {c for c in google.classes if _V1_CLASS.match(c)}
    assert "min-h-11" in google.classes or "h-14" in google.classes


# Admin.


def test_admin_table_and_user_cards_are_the_content_material() -> None:
    html, _, other = _admin_users_html()
    elements = _Elements(html)

    [table] = [e for e in elements.elements if e.tag == "table"]
    [wrapper] = [e for e in elements.with_class("mat-content") if table in e.descendants]
    assert "overflow-hidden" in wrapper.classes
    rows = [e for e in elements.elements if (e.attrs.get("id") or "").startswith("user-row-")]
    assert all("hover:bg-base-content/10" in row.classes for row in rows)
    [card] = [e for e in elements.elements if e.attrs.get("id") == f"user-card-{other.id}"]
    assert "mat-content" in card.classes


def test_admin_access_selects_are_lg_select_fields() -> None:
    html, _, _ = _admin_users_html()
    selects = [e for e in _Elements(html).elements if e.tag == "select"]

    assert len(selects) == 2
    for select in selects:
        assert {"lg-field", "lg-select"} <= select.classes
        assert "select" not in select.classes


def test_admin_toast_floats_as_thick_glass_with_a_ghost_undo() -> None:
    admin = _user("admin@example.com", UserStatus.ACTIVE)
    user = _user("athlete@example.com", UserStatus.ACTIVE)
    html = _render(
        "partials/admin/user_update.html",
        user=user,
        current_user=admin,
        pending_count=0,
        previous_status=UserStatus.PENDING,
    )
    elements = _Elements(html)

    [toast] = [e for e in elements.elements if e.attrs.get("role") == "status"]
    assert {"alert", "lgs", "lgs-thick"} <= toast.classes
    [undo] = [e for e in toast.descendants if e.tag == "button"]
    assert {"lg", "lg-ghost", "lg-sm"} <= undo.classes
    assert any(
        "absolute" in d.classes and d.attrs.get("aria-hidden") == "true" for d in undo.descendants
    )


def test_admin_status_change_error_still_shows_its_error_row() -> None:
    """AC4: a failed admin status change still shows its error row."""
    elements = _Elements(_render("partials/admin/status_change_error.html"))
    html = _render("partials/admin/status_change_error.html")

    [toast] = [e for e in elements.elements if e.attrs.get("role") == "status"]
    assert {"lgs", "lgs-thick"} <= toast.classes
    assert "text-error" in html
    assert "change your own status." in html


# The drop-a-sport dialog.


def test_drop_sport_dialog_is_thick_glass_with_lg_controls() -> None:
    sport = SimpleNamespace(id="climbing", label_key="sport.climbing.label", icon="mountain")
    goals = [SimpleNamespace(text="Send 7a")]
    module = build_templates().env.get_template("components/drop_sport_dialog.html").module
    html = str(module.drop_sport_dialog([(sport, goals)], "/onboarding", "#step"))  # type: ignore[attr-defined]
    elements = _Elements(html)

    [dialog] = [e for e in elements.elements if e.attrs.get("role") == "dialog"]
    assert {"lgs", "lgs-thick"} <= dialog.classes
    [cancel] = elements.with_class("lg", "lg-ghost")
    assert cancel.attrs.get("hx-get") == "/onboarding"
    [confirm] = elements.with_class("lg", "lg-tinted")
    assert confirm.attrs.get("type") == "submit"
