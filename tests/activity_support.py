"""The activity stores the app wires (ADR-0032, ticket #376), for repositories built in tests:
the composition root's own mapping, so a gym test reaches the gym's tables as the app does."""

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.main import build_activity_stores

STORES = build_activity_stores(default_sport_registry())
