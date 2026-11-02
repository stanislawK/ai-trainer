"""The band-order question (PRD-0010 B39, ADR-0015, ticket #431): a new band colour is placed among
the athlete's ranked bands by the athlete, never by its colour. The places are the ranks the band
can take, 1 the lightest. More places than a card offers are narrowed first, by halves, so the
heaviest place is never cut off."""

from collections.abc import Sequence

from ai_trainer.domain.clarification import (
    BAND_PURPOSE,
    ChoiceOption,
    Clarification,
    UnknownOptionError,
)

PLACE_PREFIX = "place:"
NARROW_PREFIX = "narrow:"
# The separator of a resulting order, as a hint shows it: "red, black, green".
ORDER_SEPARATOR = ", "
_SPORT = "gym"


def band_question(
    label: str, ranked: Sequence[str], *, max_options: int, window: tuple[int, int] | None = None
) -> Clarification:
    """Where `label` ranks among `ranked`, the athlete's bands lightest first. `window` is the
    places still open after narrowing taps, all of them at first."""
    lo, hi = window or (1, len(ranked) + 1)
    return Clarification(
        question="",
        options=_options(label, ranked, lo, hi, max_options),
        allow_free_text=False,
        skippable=True,
        pending=BAND_PURPOSE,
        sport=_SPORT,
        subject=label,
    )


def _options(
    label: str, ranked: Sequence[str], lo: int, hi: int, max_options: int
) -> tuple[ChoiceOption, ...]:
    if hi - lo + 1 <= max_options:
        return tuple(_place(label, ranked, rank) for rank in range(lo, hi + 1))
    # The band at rank `mid` splits the places: lighter than it, or at least as heavy.
    mid = (lo + hi) // 2
    return (
        ChoiceOption(
            id=f"{NARROW_PREFIX}{lo}-{mid}",
            copy_key="band_narrow_lighter",
            params={"band": ranked[mid - 1], "new": label},
        ),
        ChoiceOption(
            id=f"{NARROW_PREFIX}{mid + 1}-{hi}",
            copy_key="band_narrow_heavier",
            params={"band": ranked[mid - 1], "new": label},
        ),
    )


def _place(label: str, ranked: Sequence[str], rank: int) -> ChoiceOption:
    """The option that gives `label` rank `rank`: the bands from that rank on move one up."""
    order = [*ranked[: rank - 1], label, *ranked[rank - 1 :]]
    if rank == 1:
        copy_key = "band_place_first"
    elif rank == len(ranked) + 1:
        copy_key = "band_place_last"
    else:
        copy_key = "band_place_between"
    return ChoiceOption(
        id=f"{PLACE_PREFIX}{rank}",
        copy_key=copy_key,
        params={
            "band": label,
            "lighter": ranked[rank - 2] if rank > 1 else "",
            "heavier": ranked[rank - 1] if rank <= len(ranked) else "",
            "order": ORDER_SEPARATOR.join(order),
        },
    )


def places_of(question: Clarification) -> int:
    """How many places the question was built for, the heaviest rank it can still give: the
    athlete's ranked bands then, plus one. A question whose ranking has since changed no longer
    matches the athlete's bands."""
    return max(int(option.id.rsplit(":", 1)[-1].rsplit("-", 1)[-1]) for option in question.options)


def place_of(question: Clarification, option_id: str) -> int:
    """The rank the tapped place gives the band. Raises `UnknownOptionError` for an ID the
    question did not offer, or a narrowing one (ADR-0015 invariant 4)."""
    offered = {option.id for option in question.options}
    if option_id not in offered or not option_id.startswith(PLACE_PREFIX):
        raise UnknownOptionError(option_id)
    return int(option_id.removeprefix(PLACE_PREFIX))


def narrowed(
    question: Clarification, option_id: str, ranked: Sequence[str], *, max_options: int
) -> Clarification:
    """The question after a narrowing tap: the half of the places it named. Raises
    `UnknownOptionError` for an ID the question did not offer, or a place."""
    offered = {option.id for option in question.options}
    if option_id not in offered or not option_id.startswith(NARROW_PREFIX):
        raise UnknownOptionError(option_id)
    lo, hi = (int(part) for part in option_id.removeprefix(NARROW_PREFIX).split("-"))
    label = question.subject or ""
    return question.model_copy(update={"options": _options(label, ranked, lo, hi, max_options)})
