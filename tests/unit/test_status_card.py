"""The status/error card's action buttons (`components/status_card.html`, #58): a secondary
action on its own is `btn-outline` so "Sign out" doesn't read as a bare link; beside a primary
button it stays `btn-ghost`, per the Status and errors mock."""

import re

import pytest

from ai_trainer.web.templating import build_templates


def _render(template: str, **context: object) -> str:
    return build_templates().env.get_template(template).render(**context)


def _buttons(html: str) -> list[str]:
    return re.findall(r'<(?:a|button)[^>]*class="btn [^"]*"[^>]*>[^<]*', html)


@pytest.mark.parametrize("status", ["pending", "disabled"])
def test_status_page_sign_out_is_an_outline_button(status: str) -> None:
    html = _render("partials/auth/status.html", status=status, email="athlete@example.com")

    [sign_out] = _buttons(html)
    assert "Sign out" in sign_out
    assert "btn-outline" in sign_out
    assert "btn-ghost" not in sign_out


def test_500_page_keeps_a_ghost_secondary_beside_its_primary() -> None:
    html = _render("partials/errors/500.html")

    primary, secondary = _buttons(html)
    assert "btn-primary" in primary
    assert "Back to chat" in secondary
    assert "btn-ghost" in secondary
    assert "btn-outline" not in secondary


@pytest.mark.parametrize("code", ["401", "403", "404"])
def test_single_action_error_pages_show_only_their_primary_button(code: str) -> None:
    html = _render(f"partials/errors/{code}.html")

    [primary] = _buttons(html)
    assert "btn-primary" in primary
    assert "btn-outline" not in html


def test_500_page_icon_keeps_the_triangle_alert_dot() -> None:
    html = _render("partials/errors/500.html")

    assert 'stroke-linecap="round"' in html
    assert 'd="M12 17h.01"' in html
