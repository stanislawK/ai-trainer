"""A gym card's exercises in blocks (PRD 0013 B43, ticket #632; Gym Measures v2's "N rounds of"):
the neighbours of one circuit make one block that carries its rounds, and every exercise outside
a circuit is a block of its own, so a card without a circuit reads exactly as before.

Each item keeps the exercise's place in the session, which History's progress link names, and
the exercise to describe its round by: when every round is alike, the exercise with only its
first round, so `done` reads "15 reps" under "3 rounds of" rather than "3 by 15"; when they
differ, every round, read one by one."""

from collections.abc import Sequence
from dataclasses import dataclass

from ai_trainer.domain.sports.gym.payload import Exercise


@dataclass(frozen=True)
class BlockItem:
    index: int
    exercise: Exercise
    round_line: Exercise


@dataclass(frozen=True)
class ExerciseBlock:
    """`rounds` is None for an exercise outside a circuit."""

    rounds: int | None
    items: tuple[BlockItem, ...]


def _round_line(exercise: Exercise) -> Exercise:
    first = exercise.sets[0]
    if exercise.circuit is None or any(gym_set != first for gym_set in exercise.sets):
        return exercise
    return exercise.model_copy(update={"sets": [first]})


def circuit_blocks(activities: Sequence[Exercise]) -> list[ExerciseBlock]:
    blocks: list[ExerciseBlock] = []
    previous: int | None = None
    for index, exercise in enumerate(activities):
        item = BlockItem(index, exercise, _round_line(exercise))
        if exercise.circuit is not None and exercise.circuit == previous:
            last = blocks[-1]
            blocks[-1] = ExerciseBlock(last.rounds, (*last.items, item))
        else:
            blocks.append(ExerciseBlock(exercise.rounds, (item,)))
        previous = exercise.circuit
    return blocks
