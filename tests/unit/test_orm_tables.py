from ai_trainer.adapters.orm import Base

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
    "feedback_events",
    "conversations",
    "chat_messages",
    "training_sessions",
    "training_session_sources",
    "session_drafts",
    "clarifications",
    "exercises",
    "exercise_texts",
    "exercise_aliases",
    "gym_exercise_entries",
    "gym_sets",
    "athlete_bands",
}


def test_metadata_holds_every_table() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
