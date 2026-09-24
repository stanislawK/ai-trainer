"""Vendored Lucide icons, rendered inline through `templates/components/icon.html`
(ADR-0019, ticket #39). Icons are never fetched from a CDN or copied inline in templates."""

import re
from pathlib import Path

from markupsafe import Markup, escape

WEB_DIR = Path(__file__).parent
ICONS_DIR = WEB_DIR / "static" / "icons" / "lucide"

_STROKE_WIDTH = "1.75"
_VALID_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_VIEW_BOX_RE = re.compile(r'viewBox="([^"]+)"')
_ICON_CALL_RE = re.compile(r'icon\(\s*(?:name\s*=\s*)?["\']([^"\']+)["\']')


def vendored_icon_names() -> set[str]:
    return {path.stem for path in ICONS_DIR.glob("*.svg")}


def referenced_icon_names(templates_dir: Path) -> set[str]:
    """Every icon name passed to `icon(...)` across `templates_dir`'s `.html` files."""
    names: set[str] = set()
    for path in templates_dir.rglob("*.html"):
        names.update(_ICON_CALL_RE.findall(path.read_text()))
    return names


def render_icon(name: str, css_class: str = "") -> Markup:
    """Inlines a vendored Lucide SVG, forcing the monochrome stroke look (ADR-0019).

    Raises `ValueError` for a name that isn't vendored, so a typo or a missing icon fails
    the render instead of silently producing empty markup. `name` is checked against the
    kebab-case charset before it ever reaches the filesystem, so a name carrying a path
    separator (`../lucide/mountain`) or an unvendored casing can't resolve to a file that
    happens to exist outside `ICONS_DIR`, or match another icon on a case-insensitive fs.
    """
    if not _VALID_NAME_RE.fullmatch(name):
        raise ValueError(f"unknown icon {name!r}: not vendored under {ICONS_DIR}")

    path = ICONS_DIR / f"{name}.svg"
    if not path.is_file():
        raise ValueError(f"unknown icon {name!r}: not vendored under {ICONS_DIR}")

    source = path.read_text()
    view_box_match = _VIEW_BOX_RE.search(source)
    assert view_box_match is not None, f"vendored icon {name!r} has no viewBox"

    body_start = source.index(">", source.index("<svg")) + 1
    body_end = source.rindex("</svg>")
    inner = source[body_start:body_end].strip()

    return Markup(
        f'<svg viewBox="{escape(view_box_match.group(1))}" fill="none" '
        f'stroke="currentColor" stroke-width="{_STROKE_WIDTH}" '
        f'class="{escape(css_class)}" aria-hidden="true">{inner}</svg>'
    )
