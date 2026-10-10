"""The glyph on an assistant reply's avatar (PRD-0003 F15, ADR-0006, ticket #84): the one
sport's icon from `SportRegistry`, else the app mark."""

import logging
from collections.abc import Callable, Iterable

from ai_trainer.domain.sports.registry import APP_MARK, SportRegistry, UnknownSportError

logger = logging.getLogger(__name__)


def build_reply_glyph(registry: SportRegistry) -> Callable[[Iterable[str]], str]:
    """A stored sport ID the registry no longer knows (a plugin removed since) must not fail
    the page: the reply falls back to the app mark and the warning names the ID."""

    def reply_glyph(sport_ids: Iterable[str]) -> str:
        try:
            return registry.glyph_for(sport_ids)
        except UnknownSportError as error:
            logger.warning("reply stored an unknown sport %r; showing the app mark", error.sport_id)
            return APP_MARK

    return reply_glyph
