"""Vendored Lucide icons rendered through the `icon()` macro (ADR-0019, ticket #39)."""

import re
from pathlib import Path

import pytest

from ai_trainer.web.icons import WEB_DIR, referenced_icon_names, vendored_icon_names
from ai_trainer.web.templating import TEMPLATES_DIR, build_templates

_REMOTE_ICON_MARKERS = (
    "lucide.dev",
    "unpkg.com",
    "jsdelivr.net",
    "cdnjs.cloudflare.com",
    "raw.githubusercontent.com",
    "esm.sh",
)


def _render(source: str) -> str:
    templates = build_templates()
    return templates.env.from_string(source).render()


def test_icon_macro_renders_inline_svg_with_current_color_and_stroke_1_75() -> None:
    markup = _render(
        '{% from "components/icon.html" import icon %}{{ icon("mountain", "size-5") }}'
    )

    assert markup.strip().startswith("<svg")
    assert 'stroke="currentColor"' in markup
    assert 'stroke-width="1.75"' in markup
    assert 'class="size-5"' in markup
    assert 'aria-hidden="true"' in markup
    assert "<path" in markup


def test_unknown_icon_name_fails_at_render_time_instead_of_rendering_empty() -> None:
    with pytest.raises(ValueError, match="not-a-real-icon"):
        _render('{% from "components/icon.html" import icon %}{{ icon("not-a-real-icon", "") }}')


@pytest.mark.parametrize(
    "name",
    [
        "../lucide/mountain",
        "lucide/mountain",
        "MOUNTAIN",
        "Mountain",
    ],
)
def test_icon_name_outside_the_vendored_kebab_case_charset_is_rejected(name: str) -> None:
    """A name carrying a path separator can't escape `ICONS_DIR`, and a differently-cased
    name can't accidentally resolve on a case-insensitive filesystem (skeptic finding,
    ticket #39 review gate)."""
    with pytest.raises(ValueError, match="unknown icon"):
        _render(f'{{% from "components/icon.html" import icon %}}{{{{ icon("{name}", "") }}}}')


def test_no_committed_file_references_a_remote_icon_url() -> None:
    offenders = []
    for directory in (TEMPLATES_DIR, WEB_DIR / "static"):
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            contents = path.read_bytes()
            if any(marker.encode() in contents for marker in _REMOTE_ICON_MARKERS):
                offenders.append(path)

    assert offenders == []


def test_every_icon_referenced_in_templates_is_vendored() -> None:
    missing = referenced_icon_names(TEMPLATES_DIR) - vendored_icon_names()

    assert missing == set()


def test_scanner_catches_a_template_that_uses_an_unvendored_icon(tmp_path: Path) -> None:
    feature_dir = tmp_path / "pages" / "example"
    feature_dir.mkdir(parents=True)
    (feature_dir / "index.html").write_text('{{ icon("not-vendored", "size-5") }}')

    referenced = referenced_icon_names(tmp_path)

    assert "not-vendored" in referenced - vendored_icon_names()


def test_scanner_catches_a_keyword_argument_style_call(tmp_path: Path) -> None:
    """A skeptic review (ticket #39) found the scanner regex missed `icon(name=...)`
    keyword-style calls, which would let an unvendored icon slip past undetected."""
    feature_dir = tmp_path / "pages" / "example"
    feature_dir.mkdir(parents=True)
    (feature_dir / "index.html").write_text('{{ icon(name="not-vendored", class="size-5") }}')

    referenced = referenced_icon_names(tmp_path)

    assert "not-vendored" in referenced - vendored_icon_names()


def test_icon_keeps_the_vendored_round_line_caps_and_joins() -> None:
    """triangle-alert's dot is the zero-length path `M12 17h.01`, which only paints with
    `stroke-linecap="round"`; dropping the vendored caps and joins erased it (#58)."""
    markup = _render('{% from "components/icon.html" import icon %}{{ icon("triangle-alert") }}')

    assert 'stroke-linecap="round"' in markup
    assert 'stroke-linejoin="round"' in markup
    assert 'd="M12 17h.01"' in markup


def test_every_icon_call_in_templates_carries_a_size_class() -> None:
    """An unsized inline SVG collapses to 0px in a daisyUI menu and stretches to fill a dock
    item, crowding its label; the mocks' icons default to 1.25rem (`size-5`) (#58)."""
    unsized = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        for call in re.findall(r"icon\((?!name, class)[^)]*\)", path.read_text()):
            if "size-" not in call:
                unsized.append(f"{path.relative_to(TEMPLATES_DIR)}: {call}")

    assert unsized == []
