"""Decision sets (ADR-0020): question files loaded into the Pydantic type Jev answers.

A set lives at `<root>/<id>/v<N>.<locale>.yaml` and holds a `goal` and one entry per question.
Its kind picks the field type `DecisionModel` turns into a question:
- `probability`: a float bounded to 0..1, which comes back as P(yes) itself;
- `choice`: one option out of several, each with the description Jev reads instead of its name;
- `score`: ordered levels from 0, each described, which is a rubric.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    create_model,
)
from pydantic_ai import Choices

DECISIONS_ROOT = Path(__file__).parent

# Jev reads only these words, never field or option names, so none may be blank (ADR-0020).
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: _Text


class _Probability(_Question):
    kind: Literal["probability"]


class _Choice(_Question):
    kind: Literal["choice"]
    options: dict[str, _Text] = Field(min_length=2, max_length=255)


class _Score(_Question):
    kind: Literal["score"]
    levels: list[_Text] = Field(min_length=2, max_length=10)


_QuestionSpec = Annotated[_Probability | _Choice | _Score, Field(discriminator="kind")]


class _DecisionSetFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: _Text
    questions: dict[str, _QuestionSpec] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class DecisionSet:
    """A loaded set: what the `llm_calls` row names it by, and the output type to ask."""

    id: str
    version: int
    output_type: type[BaseModel]


class UnknownDecisionSetError(LookupError):
    def __init__(self, decision_id: str, version: int, locale: str) -> None:
        super().__init__(f"No decision set {decision_id!r} v{version} ({locale})")


class DecisionSetLoader:
    def __init__(self, *, root: Path = DECISIONS_ROOT) -> None:
        self._root = root

    def load(
        self,
        decision_id: str,
        version: int,
        *,
        locale: str = "en",
        options: Mapping[str, Collection[str]] | None = None,
        questions: Collection[str] | None = None,
    ) -> DecisionSet:
        """`options` keeps only the named options of a choice, in file order, for sets whose
        options depend on the athlete (their sports); the descriptions still come from the
        file. `questions` keeps only the named questions, in file order, for a caller that asks
        one rubric item of a set (a Jev eval check)."""
        path = self._root / decision_id / f"v{version}.{locale}.yaml"
        if not path.is_file():
            raise UnknownDecisionSetError(decision_id, version, locale)
        spec = _DecisionSetFile.model_validate(yaml.safe_load(path.read_text()))
        asked = _asked(spec.questions, questions)
        kept = dict(options or {})
        fields: dict[str, Any] = {
            name: _field(name, question, kept.pop(name, None)) for name, question in asked.items()
        }
        if kept:
            raise ValueError(f"Runtime options name questions the set lacks: {sorted(kept)}")
        name = "".join(part.capitalize() for part in decision_id.split("_")) + "Decision"
        output_type = create_model(name, __doc__=spec.goal, **fields)
        return DecisionSet(id=decision_id, version=version, output_type=output_type)

    def versions(self, decision_id: str, *, locale: str = "en") -> list[int]:
        """The set's versions with a file in `locale`, in order; none for an unknown ID."""
        return sorted(
            int(version)
            for path in (self._root / decision_id).glob(f"v*.{locale}.yaml")
            if (version := path.name.removeprefix("v").removesuffix(f".{locale}.yaml")).isdigit()
        )


def _asked(
    questions: dict[str, _QuestionSpec], only: Collection[str] | None
) -> dict[str, _QuestionSpec]:
    if only is None:
        return questions
    unknown = sorted(set(only) - set(questions))
    if unknown:
        raise ValueError(f"Questions not in the set: {unknown}")
    if not only:
        raise ValueError("Keep at least one question")
    return {name: question for name, question in questions.items() if name in only}


def _field(
    name: str, question: _Probability | _Choice | _Score, only: Collection[str] | None
) -> tuple[Any, Any]:
    if only is not None and not isinstance(question, _Choice):
        raise ValueError(f"Runtime options given for {name!r}, which is not a choice")
    match question:
        case _Probability():
            return float, Field(ge=0, le=1, description=question.question)
        case _Choice():
            return Choices(_kept_options(name, question.options, only), name=name), Field(
                description=question.question
            )
        case _Score():
            return _levels(question.levels), Field(description=question.question)


def _kept_options(
    name: str, options: dict[str, str], only: Collection[str] | None
) -> dict[str, str]:
    if only is None:
        return options
    unknown = sorted(set(only) - set(options))
    if unknown:
        raise ValueError(f"Runtime options for {name!r} not in its file: {unknown}")
    kept = {option: meaning for option, meaning in options.items() if option in only}
    if len(kept) < 2:
        raise ValueError(f"Runtime options for {name!r} must leave at least two options")
    return kept


def _levels(levels: list[str]) -> Any:  # noqa: ANN401 -- a runtime-built `Literal` type
    """Whole numbers 0..n, each described: the shape `DecisionModel` reads as a rubric."""
    allowed: Any = Literal[tuple(range(len(levels)))]
    schema = {
        "type": "integer",
        "anyOf": [{"const": level, "description": meaning} for level, meaning in enumerate(levels)],
    }
    return Annotated[allowed, WithJsonSchema(schema)]


__all__ = [
    "DECISIONS_ROOT",
    "DecisionSet",
    "DecisionSetLoader",
    "UnknownDecisionSetError",
]
