"""PWA installability assets (ADR-0019, ticket #42): the icons rendered from the design
system's Favicon file (mark 3b) via Playwright, and the no-service-worker invariant."""

import math
from pathlib import Path

from PIL import Image, ImageFilter

from ai_trainer.web.templating import STATIC_DIR, TEMPLATES_DIR, WEB_DIR

ICONS_DIR = STATIC_DIR / "icons"

# The mock's own maskable-safe-zone marker (`inset-[51px]` on a 512px tile) is an 80%-diameter
# circle; a glyph must stay inside it for OS icon masks to never clip it.
_SAFE_ZONE_RATIO = 0.8
# `ImageFilter.FIND_EDGES` reports spurious high contrast for the outermost few pixels (the
# convolution kernel has no neighbors beyond the image bounds), which is the tile's own
# border/rim shading, never the glyph — excluded so only real glyph-stroke edges count.
_EDGE_BORDER_MARGIN = 4
_EDGE_THRESHOLD = 40


def _glyph_bounding_radius(path: Path) -> float:
    with Image.open(path) as source:
        image = source.convert("L")
    width, height = image.size
    edges = image.filter(ImageFilter.FIND_EDGES)
    pixels = edges.load()
    assert pixels is not None

    center_x, center_y = width / 2, height / 2
    margin = _EDGE_BORDER_MARGIN
    distances: list[float] = []
    for y in range(margin, height - margin):
        for x in range(margin, width - margin):
            intensity = pixels[x, y]
            assert isinstance(intensity, int), "grayscale image yields a single-band int"
            if intensity > _EDGE_THRESHOLD:
                distances.append(math.hypot(x - center_x, y - center_y))
    assert distances, "expected at least one glyph edge pixel"
    return max(distances)


def test_icon_192_is_exactly_192_pixels_square() -> None:
    with Image.open(ICONS_DIR / "icon-192.png") as image:
        assert image.size == (192, 192)


def test_icon_512_is_exactly_512_pixels_square() -> None:
    with Image.open(ICONS_DIR / "icon-512.png") as image:
        assert image.size == (512, 512)


def test_icon_512_maskable_is_exactly_512_pixels_square() -> None:
    with Image.open(ICONS_DIR / "icon-512-maskable.png") as image:
        assert image.size == (512, 512)


def test_apple_touch_icon_is_exactly_180_pixels_square() -> None:
    with Image.open(ICONS_DIR / "apple-touch-icon.png") as image:
        assert image.size == (180, 180)


def test_maskable_icon_keeps_the_glyph_inside_the_safe_zone() -> None:
    """AC3 (ticket #42): the glyph's farthest edge pixel from center must sit inside the 80%
    safe-zone radius, or an OS icon mask could clip it."""
    path = ICONS_DIR / "icon-512-maskable.png"
    with Image.open(path) as image:
        width, _ = image.size
    safe_radius = _SAFE_ZONE_RATIO / 2 * width

    max_glyph_distance = _glyph_bounding_radius(path)

    assert max_glyph_distance <= safe_radius, (
        f"glyph reaches {max_glyph_distance:.1f}px from center, "
        f"outside the {safe_radius:.1f}px safe zone"
    )


def test_no_static_js_file_registers_a_service_worker() -> None:
    """AC4 (ticket #42, ADR-0019): the design explicitly drops offline support, so nothing
    under `static/` — hand-written (`static/js/`) or vendored (`static/vendor/`) — may call
    `serviceWorker.register`."""
    static_dir = WEB_DIR / "static"
    offenders = [
        path for path in static_dir.rglob("*.js") if "serviceWorker.register" in path.read_text()
    ]

    assert offenders == []


def test_no_template_references_a_service_worker() -> None:
    """Same invariant as above, for any inline `<script>` a template might carry."""
    offenders = [
        path
        for path in TEMPLATES_DIR.rglob("*.html")
        if "serviceWorker.register" in path.read_text()
    ]

    assert offenders == []
