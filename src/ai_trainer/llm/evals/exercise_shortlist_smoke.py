"""The `exercise_shortlist` smoke set (ADR-0034, ticket #399): `evals/smoke/exercise_shortlist.yaml`
holds typed gym names with the shortlist the app would offer, and a label that is the candidate's
name or `none`. Each case is asked of Jev once and graded on the outcome the app would reach at
the `Settings` threshold, a confident pick or the card, through the app's own `confident_pick`.
No judge and no `llm_calls` row of its own: the cases hold only typed names.

A `live` case (ticket #429) holds no candidates. It asks the app's own shortlist, on the imported
catalogue, for the typed name and passes when the exercise named in `listed` is among the
candidates: the shortlist's recall, which `EXERCISE_SEARCH_ENABLED` changes. No Jev call."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent
from pydantic_ai.models import Model

from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet
from ai_trainer.llm.exercise_shortlist import NONE_OF_THESE, confident_pick, exercise_shortlist_set


class _Candidate(BaseModel):
    name: str
    equipment: str | None = None
    target: str | None = None


class _Deps(BaseModel):
    candidates: list[_Candidate] = []
    live: bool = False


class _Expected(BaseModel):
    pick: str | None = None
    listed: str | None = None


class _Case(BaseModel):
    name: str
    inputs: EvalCaseInputs
    expected_output: _Expected

    @property
    def live(self) -> bool:
        return _Deps.model_validate(self.inputs.deps).live


# The app's shortlist for a typed name, on the imported catalogue, and the entry point's way of
# opening it (the database is wired in the composition root, not here).
type Shortlister = Callable[[str], Awaitable[Sequence[ExerciseCandidate]]]
type LiveShortlister = Callable[[], AbstractAsyncContextManager[Shortlister]]


def _check_live(smoke_set: SmokeSet, case: _Case, deps: _Deps) -> None:
    if deps.candidates or case.expected_output.pick is not None:
        raise SmokeError(f"{smoke_set.id}: live case {case.name!r} has no candidate and no pick")
    if not case.expected_output.listed:
        raise SmokeError(f"{smoke_set.id}: live case {case.name!r} needs `listed`")


def _read(smoke_set: SmokeSet) -> list[tuple[_Case, list[ExerciseCandidate]]]:
    """Every case checked before any call, so a typo in a label costs nothing."""
    raw_cases = smoke_set.dataset["cases"]
    assert isinstance(raw_cases, list)
    read: list[tuple[_Case, list[ExerciseCandidate]]] = []
    for raw in raw_cases:
        try:
            case = _Case.model_validate(raw)
            deps = _Deps.model_validate(case.inputs.deps)
        except ValidationError as error:
            raise SmokeError(f"{smoke_set.id}: {error}") from error
        if deps.live:
            _check_live(smoke_set, case, deps)
            read.append((case, []))
            continue
        # The same name is the same candidate, so a run is the same question every time.
        candidates = [
            ExerciseCandidate(id=uuid5(NAMESPACE_URL, c.name), **c.model_dump())
            for c in deps.candidates
        ]
        names = {candidate.name for candidate in candidates}
        if not candidates:
            raise SmokeError(f"{smoke_set.id}: case {case.name!r} has no candidate")
        if len(names) != len(candidates):
            raise SmokeError(f"{smoke_set.id}: case {case.name!r} lists a candidate name twice")
        if case.expected_output.pick not in {NONE_OF_THESE, *names}:
            raise SmokeError(
                f"{smoke_set.id}: case {case.name!r} labels {case.expected_output.pick!r}, "
                "which is no candidate and not 'none'"
            )
        read.append((case, candidates))
    return read


def has_live_cases(smoke_set: SmokeSet) -> bool:
    """Whether a run needs the database. A malformed set raises the `SmokeError` the run would."""
    return any(case.live for case, _ in _read(smoke_set))


async def run_exercise_shortlist_smoke(
    smoke_set: SmokeSet,
    model: Model,
    *,
    threshold: float,
    loader: DecisionSetLoader | None = None,
    shortlist: Shortlister | None = None,
) -> list[SmokeCaseResult]:
    """Asks each case once, in order. A case whose call fails fails alone."""
    results: list[SmokeCaseResult] = []
    for case, candidates in _read(smoke_set):
        if case.live:
            results.append(await _listed(smoke_set, case, shortlist))
            continue
        try:
            got = await _outcome(case, candidates, model, threshold=threshold, loader=loader)
        except Exception as error:
            results.append(_result(smoke_set, case.name, False, f"the call failed: {error}"))
            continue
        wanted = case.expected_output.pick
        detail = "" if got == wanted else f"wanted {wanted!r}, the app would take {got!r}"
        results.append(_result(smoke_set, case.name, got == wanted, detail))
    return results


async def _listed(
    smoke_set: SmokeSet, case: _Case, shortlist: Shortlister | None
) -> SmokeCaseResult:
    """Whether the app's shortlist for the typed name holds the exercise the case wants."""
    if shortlist is None:
        return _result(smoke_set, case.name, False, "a live case needs the database; none given")
    try:
        found = [candidate.name for candidate in await shortlist(case.inputs.prompt)]
    except Exception as error:
        return _result(smoke_set, case.name, False, f"the shortlist failed: {error}")
    wanted = case.expected_output.listed
    detail = "" if wanted in found else f"wanted {wanted!r} among {found}"
    return _result(smoke_set, case.name, wanted in found, detail)


async def _outcome(
    case: _Case,
    candidates: list[ExerciseCandidate],
    model: Model,
    *,
    threshold: float,
    loader: DecisionSetLoader | None,
) -> str:
    """The candidate's name the app would fill in, or `none` when its card would ask."""
    decision_set = exercise_shortlist_set(candidates, loader=loader)
    agent: Agent[None, BaseModel] = Agent(model, output_type=decision_set.output_type)
    result = await agent.run(case.inputs.prompt)
    distributions: Mapping[str, Mapping[str, float]] = (result.response.provider_details or {}).get(
        "probabilities", {}
    )
    option = confident_pick(result.output, distributions, threshold=threshold)
    names = {str(candidate.id): candidate.name for candidate in candidates}
    return NONE_OF_THESE if option is None else names[option]


def _result(smoke_set: SmokeSet, name: str, passed: bool, detail: str) -> SmokeCaseResult:
    return SmokeCaseResult(
        name=name, passed=passed, detail=detail, gap=smoke_set.known_gaps.get(name)
    )
