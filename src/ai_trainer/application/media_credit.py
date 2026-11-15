"""How a catalogue picture is credited (ADR-0031 *First media source*, *Fallback media source*),
shared by the admin's review (#554) and the exercise card (#558): where it came from, wger's
credit split so its licence can be a link, wger's image page, and whether wger flagged it as
AI-made."""

import re
from dataclasses import dataclass
from typing import Literal

from ai_trainer.domain.media import MediaAsset, MediaDelivery
from ai_trainer.domain.media_generation import GENERATED_PICTURE_SOURCE
from ai_trainer.domain.media_sources import AI_GENERATED_FRAGMENT, WGER_SOURCE

type PictureOrigin = Literal["wger", "generated", "embedded", "other"]

# ADR-0031 *First media source*: `<author> · <licence name> (<licence URL>) — via wger.de`.
_WGER_ATTRIBUTION = re.compile(
    r"^(?P<author>.+) · (?P<licence>.+) \((?P<url>https?://[^\s()]+)\) — via wger\.de$"
)


@dataclass(frozen=True, slots=True)
class WgerCredit:
    """A wger picture's credit, split so its licence can be a link."""

    author: str
    licence: str
    licence_url: str


def picture_origin(asset: MediaAsset) -> PictureOrigin:
    if asset.delivery is MediaDelivery.EMBEDDED:
        return "embedded"
    if asset.source == WGER_SOURCE:
        return "wger"
    if asset.source == GENERATED_PICTURE_SOURCE:
        return "generated"
    return "other"


def wger_credit(asset: MediaAsset) -> WgerCredit | None:
    """None for any other source, or a wger attribution not in ADR-0031's form."""
    if picture_origin(asset) != "wger":
        return None
    match = _WGER_ATTRIBUTION.fullmatch(asset.attribution or "")
    if match is None:
        return None
    return WgerCredit(author=match["author"], licence=match["licence"], licence_url=match["url"])


def page_url(asset: MediaAsset) -> str | None:
    """wger's image page, or an embedded video's own URL."""
    if picture_origin(asset) == "wger":
        return asset.source_ref.removesuffix(AI_GENERATED_FRAGMENT)
    return asset.embed_url


def ai_flagged(asset: MediaAsset) -> bool:
    """A wger picture its uploader marked as AI-made."""
    return picture_origin(asset) == "wger" and asset.source_ref.endswith(AI_GENERATED_FRAGMENT)
