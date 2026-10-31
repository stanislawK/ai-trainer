from pathlib import Path

import ai_trainer.adapters.orm as orm_package
from ai_trainer.adapters.orm import Base

MAX_MODULE_LINES = 400

EXPECTED_TABLES = {
    "users",
    "sessions",
    "user_status_changes",
    "user_sports",
    "weekly_availability",
    "goals",
    "athlete_profiles",
    "sport_profiles",
    "onboarding_answers",
    "llm_calls",
    "conversations",
    "chat_messages",
    "training_sessions",
    "training_session_sources",
    "session_drafts",
    "clarifications",
    "exercises",
    "exercise_texts",
    "exercise_aliases",
}


def test_metadata_holds_every_table() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_orm_module_is_over_the_size_rule() -> None:
    package_dir = Path(orm_package.__file__).parent
    modules = sorted(package_dir.rglob("*.py"))

    assert package_dir.name == "orm"
    assert modules
    for module in modules:
        lines = len(module.read_text().splitlines())
        assert lines <= MAX_MODULE_LINES, f"{module.name} has {lines} lines"
