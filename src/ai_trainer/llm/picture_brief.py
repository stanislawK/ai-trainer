"""`media.picture_brief` (ADR-0031 *Picture generation*, #532): a text model turns an exercise's
name and public description into the brief an image model draws from. The brief names the start
and end position, one figure on a plain background, and no printed text, since a picture drawn
from a bare name printed wrong labels (docs/research/exercise-media.md finding 8).

Its input is public text only, never athlete data: the catalogue's `exercise_texts` and the
passages the run's own `search_knowledge` returned. An exercise with neither gets no brief, and
no model call (`needs_no_model`). The deps, the brief and that check are in
`domain/media_generation.py`; the template, its grader and the writer are here."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, EvaluatorOutput

from ai_trainer.application.ports.llm_gateway import LlmGatewayPort
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.media_generation import BriefFailed, NoBrief, PictureBrief, PictureBriefDeps
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate

PICTURE_BRIEF_TEMPLATE_ID = "media.picture_brief"
PICTURE_BRIEF_VERSION = 1

# The brief's image prompt forbids printed text in so many words: "no" or "without" right
# before the text it forbids, not anywhere earlier in the sentence.
_NO_TEXT_RULE = re.compile(
    r"\b(no|without)\s+(any\s+)?(printed\s+|written\s+)?(text|labels?|letters|words)\b",
    re.IGNORECASE,
)
# Fillers say nothing about which position a prompt describes. A word is compared by its stem
# (no plural "s", first five letters), so "arms" meets "arm" and "extended" meets "extending".
# A position is restated when half its other words are in the prompt.
_MIN_KEY_WORD = 3
_STEM_LENGTH = 5
_FILLERS = frozenset(
    {
        "the", "and", "for", "its", "his", "her", "one", "with", "from", "then", "that",
        "this", "into", "onto", "over", "your", "their", "each",
    }
)  # fmt: skip
_RESTATED_SHARE = 0.5
_WORD = re.compile(r"[a-z]+")


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


class LlmPictureBriefWriter:
    """Implements `PictureBriefWriterPort` (#533): the template rendered as its smoke set runs
    it, through the text gateway, which writes the call's `llm_calls` row (ADR-0018). The user
    turn is the exercise's name, a public value, so no prompt text lives in Python (ADR-0008)."""

    def __init__(self, *, registry: PromptRegistry, gateway: LlmGatewayPort, model: str) -> None:
        self._registry = registry
        self._gateway = gateway
        self._model = model

    async def write(
        self, *, user_id: UUID, deps: PictureBriefDeps
    ) -> PictureBrief | NoBrief | BriefFailed:
        template = self._registry.get(PICTURE_BRIEF_TEMPLATE_ID, PICTURE_BRIEF_VERSION)
        result = await self._gateway.run(
            user_id=user_id,
            template_id=template.id,
            template_version=template.version,
            model_id=self._model,
            output_type=(PictureBrief, NoBrief),
            instructions=self._registry.render_instructions(template.id, template.version, deps),
            prompt=deps.exercise_name,
            temperature=template.temperature,
            output_retries=template.output_retries,
        )
        if isinstance(result.output, PictureBrief | NoBrief):
            return result.output
        return BriefFailed(
            reason="timeout" if result.outcome is LlmCallOutcome.TIMEOUT else "error"
        )


def _key_words(text: str) -> set[str]:
    return {
        word.removesuffix("s")[:_STEM_LENGTH]
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
