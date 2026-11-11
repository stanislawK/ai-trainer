"""A stored picture's public URL is the media base plus its key (ADR-0031, ADR-0033, #553)."""

from ai_trainer.domain.object_storage import media_public_url

KEY = "exercises/42/abc123.jpg"


def test_no_base_gives_no_url() -> None:
    assert media_public_url(None, KEY) is None


def test_the_url_is_the_base_plus_the_key() -> None:
    assert media_public_url("https://media.example.com", KEY) == (
        "https://media.example.com/exercises/42/abc123.jpg"
    )


def test_trailing_slash_makes_no_difference() -> None:
    assert media_public_url("https://media.example.com/", KEY) == media_public_url(
        "https://media.example.com", KEY
    )
    assert media_public_url("http://localhost:7070/ai-trainer-media/", KEY) == (
        "http://localhost:7070/ai-trainer-media/exercises/42/abc123.jpg"
    )


def test_a_key_is_percent_encoded_with_its_slashes_kept() -> None:
    assert media_public_url("https://m.example.com", "exercises/a b/c#d.jpg") == (
        "https://m.example.com/exercises/a%20b/c%23d.jpg"
    )
