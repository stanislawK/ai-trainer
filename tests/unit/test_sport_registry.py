"""SportRegistry: the only list of sports (ADR-0006, ticket #73)."""

import pytest

from ai_trainer.domain.sports.base import SportPlugin
from ai_trainer.domain.sports.registry import (
    APP_MARK,
    SportRegistry,
    UnknownSportError,
    default_sport_registry,
)
from ai_trainer.web.icons import vendored_icon_names


def test_sport_registry_lists_climbing_gym_cycling_in_order() -> None:
    registry = default_sport_registry()

    assert [(plugin.id, plugin.icon) for plugin in registry.all()] == [
        ("climbing", "mountain"),
        ("gym", "dumbbell"),
        ("cycling", "bike"),
    ]


def test_sport_registry_declares_a_label_key_and_no_english_label() -> None:
    labels = {plugin.id: plugin.label_key for plugin in default_sport_registry().all()}

    assert labels == {
        "climbing": "sport.climbing.label",
        "gym": "sport.gym.label",
        "cycling": "sport.cycling.label",
    }


def test_sport_registry_icons_are_vendored() -> None:
    icons = {plugin.icon for plugin in default_sport_registry().all()}

    assert icons <= vendored_icon_names()


def test_sport_registry_gets_a_sport_by_id() -> None:
    assert default_sport_registry().get("gym").icon == "dumbbell"


@pytest.mark.parametrize(
    ("sport_ids", "expected"),
    [
        (["climbing"], "mountain"),
        (["gym"], "dumbbell"),
        (["cycling"], "bike"),
        ([], APP_MARK),
        (["climbing", "cycling"], APP_MARK),
        (["gym", "gym"], "dumbbell"),
    ],
)
def test_sport_registry_glyph_is_the_icon_for_one_sport_else_the_app_mark(
    sport_ids: list[str], expected: str
) -> None:
    glyph = default_sport_registry().glyph_for(sport_ids)

    print(f"{sport_ids} -> {glyph}")
    assert glyph == expected


def test_sport_registry_unknown_id_raises_domain_error() -> None:
    registry = default_sport_registry()

    with pytest.raises(UnknownSportError, match="rowing"):
        registry.get("rowing")
    with pytest.raises(UnknownSportError, match="rowing"):
        registry.glyph_for(["climbing", "rowing"])


def test_sport_registry_lists_a_fourth_plugin_added_in_a_fixture() -> None:
    rowing = SportPlugin(id="rowing", label_key="sport.rowing.label", icon="route")
    registry = SportRegistry([*default_sport_registry().all(), rowing])

    assert [plugin.id for plugin in registry.all()] == ["climbing", "gym", "cycling", "rowing"]
    assert registry.get("rowing") is rowing
    assert registry.glyph_for(["rowing"]) == "route"


def test_sport_registry_rejects_a_duplicate_id() -> None:
    plugin = SportPlugin(id="gym", label_key="sport.gym.label", icon="dumbbell")

    with pytest.raises(ValueError, match="gym"):
        SportRegistry([plugin, plugin])
