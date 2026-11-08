"""A media asset refuses the combinations `media_assets`' CHECKs refuse (ADR-0031, F24)."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.media import MediaAsset, MediaDelivery, MediaKind, MediaRights

SHA256 = "a" * 64


def _stored(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": MediaKind.IMAGE,
        "rights": MediaRights.LICENSED,
        "delivery": MediaDelivery.STORED,
        "storage_key": "wger/1.png",
        "embed_url": None,
        "source": "wger",
        "source_ref": "https://wger.de/api/v2/exerciseimage/1/",
        "license": "CC BY-SA 4.0",
        "attribution": "Author · CC BY-SA 4.0 — via wger.de",
        "sha256": SHA256,
        "width": 800,
        "height": 600,
    } | overrides


def _embedded(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": MediaKind.VIDEO,
        "rights": MediaRights.UNSTATED,
        "delivery": MediaDelivery.EMBEDDED,
        "storage_key": None,
        "embed_url": "https://www.youtube.com/embed/abc",
        "source": "youtube",
        "source_ref": "abc",
        "license": None,
        "attribution": None,
        "sha256": None,
    } | overrides


def test_a_stored_licensed_asset_and_an_embedded_unstated_one_are_valid() -> None:
    stored = MediaAsset(**_stored())
    embedded = MediaAsset(**_embedded())

    assert stored.id is None
    assert stored.delivery is MediaDelivery.STORED
    assert embedded.width is None


@pytest.mark.parametrize(
    "fields",
    [
        _stored(sha256=None),
        _stored(storage_key=None),
        _stored(embed_url="https://example.com/x.png"),
        _stored(attribution=None),
        _stored(license=None),
        _stored(rights=MediaRights.UNSTATED, license=None, attribution=None),
        _embedded(embed_url=None),
        _embedded(storage_key="wger/1.png"),
        _stored(sha256="not-a-digest"),
        _stored(width=0),
        _stored(height=-1),
        _stored(source=""),
        _stored(source_ref=""),
    ],
    ids=[
        "stored-no-sha256",
        "stored-no-key",
        "stored-with-embed",
        "licensed-no-attribution",
        "licensed-no-license",
        "unstated-stored",
        "embedded-no-url",
        "embedded-with-key",
        "bad-sha256",
        "zero-width",
        "negative-height",
        "blank-source",
        "blank-source-ref",
    ],
)
def test_an_asset_the_checks_refuse_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MediaAsset(**fields)
