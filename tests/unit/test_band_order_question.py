"""The band-order question (PRD-0010 B39, ADR-0015, ticket #431): where a new band colour ranks
among the athlete's ranked bands, as a choice card of concrete places, narrowed first when
there are more places than a card offers."""

import pytest

from ai_trainer.domain.clarification import BAND_PURPOSE, UnknownOptionError
from ai_trainer.domain.sports.gym.bands import (
    band_question,
    narrowed,
    place_of,
)


def test_a_second_band_offers_lighter_and_heavier_than_the_ranked_one() -> None:
    question = band_question("black", ["red"], max_options=5)

    assert [option.id for option in question.options] == ["place:1", "place:2"]
    first, last = question.options
    assert (first.copy_key, first.params["heavier"]) == ("band_place_first", "red")
    assert (last.copy_key, last.params["lighter"]) == ("band_place_last", "red")
    assert first.params["order"] == "black < red"
    assert last.params["order"] == "red < black"
    assert question.subject == "black"
    assert question.sport == "gym"
    assert question.pending == BAND_PURPOSE


def test_a_place_between_two_bands_names_both_and_the_resulting_order() -> None:
    question = band_question("black", ["red", "green"], max_options=5)

    between = question.options[1]
    assert between.copy_key == "band_place_between"
    assert (between.params["lighter"], between.params["heavier"]) == ("red", "green")
    assert between.params["order"] == "red < black < green"


def test_the_card_never_asks_to_skip_by_typing() -> None:
    question = band_question("black", ["red"], max_options=5)

    assert question.allow_free_text is False
    assert question.skippable is True


def test_more_ranked_bands_than_the_cap_narrow_first() -> None:
    ranked = ["b1", "b2", "b3", "b4", "b5", "b6"]

    question = band_question("new", ranked, max_options=5)

    assert [option.id for option in question.options] == ["narrow:1-4", "narrow:5-7"]
    assert [option.copy_key for option in question.options] == [
        "band_narrow_lighter",
        "band_narrow_heavier",
    ]
    assert question.options[0].params["band"] == "b4"


def test_a_narrowing_tap_keeps_the_heaviest_place_reachable() -> None:
    ranked = ["b1", "b2", "b3", "b4", "b5", "b6"]
    question = band_question("new", ranked, max_options=5)

    heavier = narrowed(question, "narrow:5-7", ranked, max_options=5)

    assert [option.id for option in heavier.options] == ["place:5", "place:6", "place:7"]
    assert heavier.options[-1].copy_key == "band_place_last"


def test_a_window_still_too_wide_narrows_again() -> None:
    ranked = [f"b{n}" for n in range(1, 10)]
    question = band_question("new", ranked, max_options=3)

    inner = narrowed(question, question.options[0].id, ranked, max_options=3)

    assert all(option.id.startswith("narrow:") for option in inner.options)


def test_a_place_the_question_never_offered_is_an_unknown_option() -> None:
    question = band_question("black", ["red"], max_options=5)

    with pytest.raises(UnknownOptionError):
        place_of(question, "place:9")
    with pytest.raises(UnknownOptionError):
        narrowed(question, "place:1", ["red"], max_options=5)


def test_a_place_the_question_offered_is_its_rank() -> None:
    question = band_question("black", ["red", "green"], max_options=5)

    assert place_of(question, "place:2") == 2
