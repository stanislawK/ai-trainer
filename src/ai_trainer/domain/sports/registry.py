"""`SportRegistry` is the only list of sports (ADR-0006 invariant 5)."""

from collections.abc import Iterable, Sequence

from ai_trainer.domain.sports.base import SportPlugin

# Stands in for the glyph when a reply is about no sport or several (PRD-0003 F15). It is not
# a Lucide icon; templates render it as the app mark.
APP_MARK = "app-mark"


class UnknownSportError(Exception):
    def __init__(self, sport_id: str) -> None:
        super().__init__(f"unknown sport {sport_id!r}")
        self.sport_id = sport_id


class SportRegistry:
    def __init__(self, plugins: Iterable[SportPlugin]) -> None:
        self._plugins: dict[str, SportPlugin] = {}
        for plugin in plugins:
            if plugin.id in self._plugins:
                raise ValueError(f"duplicate sport id {plugin.id!r}")
            self._plugins[plugin.id] = plugin

    def all(self) -> Sequence[SportPlugin]:
        """Every sport, in registration order."""
        return tuple(self._plugins.values())

    def get(self, sport_id: str) -> SportPlugin:
        try:
            return self._plugins[sport_id]
        except KeyError:
            raise UnknownSportError(sport_id) from None

    def glyph_for(self, sport_ids: Iterable[str]) -> str:
        """The sport's icon when exactly one distinct sport is involved, else the app mark."""
        plugins = {self.get(sport_id) for sport_id in sport_ids}
        if len(plugins) == 1:
            return plugins.pop().icon
        return APP_MARK


def default_sport_registry() -> SportRegistry:
    return SportRegistry(
        [
            SportPlugin(id="climbing", label_key="sport.climbing.label", icon="mountain"),
            SportPlugin(id="gym", label_key="sport.gym.label", icon="dumbbell"),
            SportPlugin(id="cycling", label_key="sport.cycling.label", icon="bike"),
        ]
    )
