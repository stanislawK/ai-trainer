"""A gym card's exercises in blocks (ticket #632): neighbours in one circuit make one block with
its rounds, each other exercise a block of its own, each keeping its place in the session."""

import re
from typing import Any

from ai_trainer.domain.sports.gym.payload import Exercise
from ai_trainer.web.gym_blocks import circuit_blocks
from ai_trainer.web.templating import build_templates


def _exercise(name: str, *reps: int, circuit: int | None = None) -> Exercise:
    data: dict[str, Any] = {
        "name_as_typed": name,
        "measure": "reps",
        "sets": [{"reps": count} for count in reps],
    }
    if circuit is not None:
        data |= {"circuit": circuit, "rounds": len(reps)}
    return Exercise.model_validate(data)


def test_plain_exercises_are_one_block_each_with_no_rounds() -> None:
    squat, plank = _exercise("squat", 10, 10), _exercise("plank", 30)

    blocks = circuit_blocks([squat, plank])

    assert [(block.rounds, [item.index for item in block.items]) for block in blocks] == [
        (None, [0]),
        (None, [1]),
    ]
    assert blocks[0].items[0].exercise is squat
    assert blocks[0].items[0].round_line is squat


def test_a_circuits_neighbours_share_one_block_with_its_rounds() -> None:
    blocks = circuit_blocks(
        [
            _exercise("burpees", 10, 10, 10, circuit=1),
            _exercise("squats", 15, 15, 15, circuit=1),
            _exercise("plank", 30),
            _exercise("lunges", 8, 8, circuit=2),
        ]
    )

    assert [(block.rounds, [item.index for item in block.items]) for block in blocks] == [
        (3, [0, 1]),
        (None, [2]),
        (2, [3]),
    ]


def test_a_round_line_is_one_round_when_every_round_is_alike() -> None:
    (block,) = circuit_blocks([_exercise("burpees", 10, 10, 10, circuit=1)])

    (item,) = block.items

    assert [gym_set.reps for gym_set in item.round_line.sets] == [10]
    assert len(item.exercise.sets) == 3


def test_a_round_line_keeps_every_round_when_they_differ() -> None:
    (block,) = circuit_blocks([_exercise("burpees", 10, 8, 6, circuit=1)])

    (item,) = block.items

    assert [gym_set.reps for gym_set in item.round_line.sets] == [10, 8, 6]


# --- The templates that use the blocks -------------------------------------------------------


def _render(source: str, activities: list[Exercise]) -> str:
    return build_templates().env.from_string(source).render(activities=activities)


_CARD = '{% from "components/gym_copy.html" import exercise_blocks %}'
_CARD += "{{ exercise_blocks(activities) }}"
_EDIT = '{% from "components/gym_edit_fields.html" import gym_edit_fields %}'
_EDIT += '{{ gym_edit_fields(activities, {}, [], "d1") }}'


def test_a_plain_entrys_card_markup_is_as_before_circuits() -> None:
    card = _render(_CARD, [_exercise("squat", 10, 10, 10)])

    assert card == (
        '<ul class="flex flex-col divide-y divide-base-content/10" aria-label="Exercises">\n'
        '  <li class="flex flex-col gap-0.5 py-3 first:pt-0 last:pb-0">\n'
        '    <span class="text-[16px] font-medium lbl-1">Squat</span>\n'
        '    <span class="text-[13px] lbl-2 tabular-nums">3 \N{MULTIPLICATION SIGN} 10</span>\n'
        "  </li>\n"
        "</ul>"
    )


def test_a_circuits_card_lists_each_exercises_round_under_its_rounds() -> None:
    card = _render(
        _CARD,
        [_exercise("burpees", 10, 10, 10, circuit=1), _exercise("squats", 15, 12, 10, circuit=1)],
    )

    text = " ".join(re.sub(r"<[^>]+>", " ", card).split())
    assert text == "3 rounds of 1 Burpees 10 reps 2 Squats 15 · 12 · 10"


def test_a_one_round_circuit_reads_round_not_rounds() -> None:
    card = _render(_CARD, [_exercise("burpees", 10, circuit=1), _exercise("squats", 15, circuit=1)])
    listed = _render(
        '{% from "components/gym_copy.html" import rounds_word %}{{ rounds_word(1) }}', []
    )

    text = " ".join(re.sub(r"<[^>]+>", " ", card).split())
    assert text.startswith("1 round of ")
    assert listed == "1 round"


def test_the_edit_form_has_one_rounds_field_above_a_circuits_exercises() -> None:
    form = _render(
        _EDIT,
        [
            _exercise("plank", 30),
            _exercise("burpees", 10, 10, 10, circuit=1),
            _exercise("squats", 15, 15, 15, circuit=1),
        ],
    )

    circuit = form[form.index("data-circuit") :]
    assert re.findall(r'name="activities\.\d+\.rounds"', form) == ['name="activities.1.rounds"']
    assert re.search(r'name="activities\.1\.rounds"[^>]*value="3"[^>]*data-rounds', form)
    assert circuit.index("Rounds") < circuit.index('name="activities.1.name"')
    assert len(re.findall(r'name="activities\.2\.sets\.\d+\.reps"', form)) == 3


def test_a_circuits_exercises_have_no_add_or_remove_set() -> None:
    form = _render(
        _EDIT,
        [_exercise("plank", 30), _exercise("burpees", 10, 10, circuit=1)],
    )

    plain, circuit = form.split("data-circuit", 1)
    assert "data-add-set" in plain
    assert "data-remove-set" in plain
    assert "data-set-template" in plain
    assert "data-add-set" not in circuit
    assert "data-remove-set" not in circuit
    assert "data-set-template" not in circuit
