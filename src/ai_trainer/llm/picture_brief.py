"""`media.picture_brief` (ADR-0031 *Picture generation*, #532): a text model turns an exercise's
name and public description into the brief an image model draws from. The brief names the start
and end position, one figure on a plain background, and no printed text, since a picture drawn
from a bare name printed wrong labels (docs/research/exercise-media.md finding 8).

Its input is public text only, never athlete data: the catalogue's `exercise_texts` and the
passages the run's own `search_knowledge` returned. An exercise with neither gets no brief, and
no model call."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, EvaluatorOutput

from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.knowledge_search import KnowledgeSearchFailed, KnowledgeSearchPage
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.prompts.template import PromptTemplate

PICTURE_BRIEF_TEMPLATE_ID = "media.picture_brief"
PICTURE_BRIEF_VERSION = 1

# The brief's image prompt forbids printed text in so many words.
_NO_TEXT_RULE = re.compile(r"\b(no|without)\b[^.]*\b(text|labels?|letters|words)\b", re.IGNORECASE)
# A word this short ("the", "arm") says nothing about which position a prompt describes, and
# neither do these fillers; a position is restated when half its other words are in the prompt.
_MIN_KEY_WORD = 4
_FILLERS = frozenset(
    {"with", "from", "then", "that", "this", "into", "onto", "over", "your", "their", "each"}
)
_RESTATED_SHARE = 0.5
_WORD = re.compile(r"[a-z]+")


class BriefPassage(BaseModel):
    """One knowledge passage about the exercise, as the template sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    text: str


class PictureBriefDeps(BaseModel):
    """The template's variables: the exercise's name and public texts, nothing else. Extra
    fields are refused, so no identity can ride along (ADR-0031)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exercise_name: str
    description: str | None
    passages: list[BriefPassage]


class PictureBrief(BaseModel):
    """What the image model is given: the two positions, and the prompt that restates them
    with the drawing rules."""

    exercise_name: str
    start_position: str
    end_position: str
    image_prompt: str


class NoBrief(BaseModel):
    """The texts don't say how the exercise is done, so nothing is drawn."""

    reason: str


def _description(text: ExerciseText | None) -> str | None:
    if text is None:
        return None
    if text.instructions.strip():
        return text.instructions.strip()
    return "\n".join(step.strip() for step in text.steps if step.strip()) or None


def picture_brief_deps(
    exercise_name: str,
    text: ExerciseText | None,
    search: KnowledgeSearchPage | KnowledgeSearchFailed,
) -> PictureBriefDeps:
    """The deps for one exercise: its catalogue text, and only the passages its own search
    returned, best first. A failed search gives none."""
    hits = search.hits if isinstance(search, KnowledgeSearchPage) else ()
    return PictureBriefDeps(
        exercise_name=exercise_name,
        description=_description(text),
        passages=[BriefPassage(title=hit.title, text=hit.text) for hit in hits],
    )


def needs_no_model(deps: PictureBriefDeps) -> NoBrief | None:
    """`NoBrief` when there is nothing to draw from but the name (ADR-0031): no model is
    called then. `None` when the template should run."""
    if (deps.description or "").strip() or deps.passages:
        return None
    return NoBrief(reason="no exercise text and no knowledge passage")


def picture_brief_template() -> PromptTemplate[PictureBriefDeps, PictureBrief | NoBrief]:
    return PromptTemplate(
        id=PICTURE_BRIEF_TEMPLATE_ID,
        version=PICTURE_BRIEF_VERSION,
        locale="en",
        deps_type=PictureBriefDeps,
        output_type=(PictureBrief, NoBrief),
        model_settings_key="picture_brief_model",
        # The output is read by code: the same texts should give the same brief twice.
        temperature=0,
        # It never writes to the athlete.
        persona=False,
    )


def _key_words(text: str) -> set[str]:
    return {
        word
        for word in _WORD.findall(text.lower())
        if len(word) >= _MIN_KEY_WORD and word not in _FILLERS
    }


def _named_in(position: str, prompt: str) -> bool:
    words = _key_words(position)
    if not words:
        return False
    return len(words & _key_words(prompt)) >= _RESTATED_SHARE * len(words)


def _forbids(metadata: object) -> Sequence[str]:
    if not isinstance(metadata, dict):
        return ()
    expect = metadata.get("expect")
    forbids = expect.get("forbids") if isinstance(expect, dict) else None
    return [str(pattern) for pattern in forbids] if isinstance(forbids, list) else ()


@dataclass
class PictureBriefMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (ADR-0034): a brief exactly where one is due; then the name unchanged, both
    positions named and restated in the image prompt, the no-text rule stated, and none of the
    case's forbidden details (a passage that contradicts the exercise text) copied."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        if not isinstance(ctx.expected_output, PictureBrief):
            return {"kind": isinstance(ctx.output, NoBrief)}
        if not isinstance(ctx.output, PictureBrief):
            return {"kind": False}
        brief = ctx.output
        deps = PictureBriefDeps.model_validate(ctx.inputs.deps)
        drawn = " ".join((brief.start_position, brief.end_position, brief.image_prompt))
        return {
            "kind": True,
            "name_unchanged": brief.exercise_name.strip() == deps.exercise_name.strip(),
            "positions_named": _named_in(brief.start_position, brief.image_prompt)
            and _named_in(brief.end_position, brief.image_prompt),
            "no_text_rule": bool(_NO_TEXT_RULE.search(brief.image_prompt)),
            "forbids": not any(
                re.search(pattern, drawn, re.IGNORECASE) for pattern in _forbids(ctx.metadata)
            ),
        }
