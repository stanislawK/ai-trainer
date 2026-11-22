"""`exercise.simple_howto` (ADR-0031 *Beginner how-to*, F23, #589): a text model shortens a
catalogue exercise's English instructions into two plain lines for a beginner, keeping every
safety and form cue and adding nothing.

Its input is public catalogue text only: the name, the instructions, the target and the
equipment. The deps, the outputs and the code checks are in `domain/exercise_simple_texts.py`;
the template, its grader and the writer are here."""

from dataclasses import dataclass
from typing import Self
from uuid import UUID

from pydantic import BaseModel, model_validator
from pydantic_ai import ModelRetry
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, EvaluatorOutput

from ai_trainer.application.ports.llm_gateway import LlmGatewayPort
from ai_trainer.domain.exercise_simple_texts import (
    HowtoCallFailed,
    NoHowto,
    SimpleHowto,
    SimpleHowtoDeps,
    SimpleHowtoDraft,
    SimpleHowtoRetryDeps,
    has_new_digit,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences, ReasoningEffort
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate, TextRenderer
from ai_trainer.settings_values import DEFAULT_EXERCISE_SIMPLE_HOWTO_MAX_CHARS

SIMPLE_HOWTO_TEMPLATE_ID = "exercise.simple_howto"
SIMPLE_HOWTO_VERSION = 2
# The note a reply over the cap goes back with, once, in the same run (#600, ADR-0031).
SIMPLE_HOWTO_RETRY_ID = "exercise.simple_howto.retry"
SIMPLE_HOWTO_RETRY_VERSION = 1


def simple_howto_output(
    deps: BaseModel, texts: TextRenderer
) -> tuple[type[SimpleHowto], type[NoHowto]]:
    """The template's output check (#600): `SimpleHowto` refusing a stripped text over the deps'
    `max_chars` with `ModelRetry`, so the model rewrites it within `output_retries` in the same
    run, one gateway call and one `llm_calls` row. `NoHowto` is never sent back.

    The checked type keeps `SimpleHowto`'s name, docstring and fields, so the model sees the same
    output tool, and it is a `SimpleHowto`, so callers read it unchanged."""
    limit = SimpleHowtoDeps.model_validate(deps).max_chars

    class Checked(SimpleHowto):
        __doc__ = SimpleHowto.__doc__

        @model_validator(mode="after")
        def _within_cap(self) -> Self:
            length = len(self.text.strip())
            if length > limit:
                raise ModelRetry(
                    texts.render_text(
                        SIMPLE_HOWTO_RETRY_ID,
                        SIMPLE_HOWTO_RETRY_VERSION,
                        SimpleHowtoRetryDeps(length=length, limit=limit),
                    )
                )
            return self

    # The output tool's name and schema title come from the class name.
    Checked.__name__ = Checked.__qualname__ = SimpleHowto.__name__
    return Checked, NoHowto


def simple_howto_template() -> PromptTemplate[SimpleHowtoDeps, SimpleHowto | NoHowto]:
    return PromptTemplate(
        id=SIMPLE_HOWTO_TEMPLATE_ID,
        version=SIMPLE_HOWTO_VERSION,
        locale="en",
        deps_type=SimpleHowtoDeps,
        output_type=(SimpleHowto, NoHowto),
        model_settings_key="simple_howto_model",
        provider_settings_key="simple_howto_provider_preferences",
        reasoning_effort_settings_key="simple_howto_reasoning_effort",
        # Stored once and shown to every athlete: the same text should give the same how-to.
        temperature=0,
        # A catalogue text, not a reply to the athlete.
        persona=False,
        output_check=simple_howto_output,
    )


@dataclass(frozen=True, slots=True)
class SimpleHowtoCall:
    """The template's call settings from `Settings` (ADR-0026)."""

    model: str
    provider: ProviderPreferences | None
    reasoning_effort: ReasoningEffort | None
    timeout_seconds: float


class LlmSimpleHowtoWriter:
    """Implements `SimpleHowtoWriterPort`: the template rendered as its smoke set runs it,
    through the text gateway, which writes the call's `llm_calls` row under the athlete
    (ADR-0018). The user turn is the exercise's name, a public value, so no prompt text lives in
    Python (ADR-0008)."""

    def __init__(
        self, *, registry: PromptRegistry, gateway: LlmGatewayPort, call: SimpleHowtoCall
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._call = call

    async def write(
        self, *, user_id: UUID, deps: SimpleHowtoDeps
    ) -> SimpleHowtoDraft | NoHowto | HowtoCallFailed:
        template = self._registry.get(SIMPLE_HOWTO_TEMPLATE_ID, SIMPLE_HOWTO_VERSION)
        result = await self._gateway.run(
            user_id=user_id,
            template_id=template.id,
            template_version=template.version,
            model_id=self._call.model,
            # The template's own check, as its smoke set runs it.
            output_type=simple_howto_output(deps, self._registry),
            instructions=self._registry.render_instructions(template.id, template.version, deps),
            prompt=deps.exercise_name,
            temperature=template.temperature,
            output_retries=template.output_retries,
            provider=self._call.provider,
            reasoning_effort=self._call.reasoning_effort,
            timeout_seconds=self._call.timeout_seconds,
        )
        if isinstance(result.output, SimpleHowto):
            return SimpleHowtoDraft(
                text=result.output.text,
                template_id=template.id,
                template_version=template.version,
                model=self._call.model,
            )
        if isinstance(result.output, NoHowto):
            return result.output
        match result.outcome:
            case LlmCallOutcome.TIMEOUT:
                return HowtoCallFailed(reason="timeout")
            case LlmCallOutcome.RATE_LIMITED:
                return HowtoCallFailed(reason="rate_limited")
            case _:
                return HowtoCallFailed(reason="error")


@dataclass
class SimpleHowtoMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (ADR-0034): a how-to exactly where one is due, then non-empty and within the
    card's length, with no digit the source lacks. Whether every cue survived and nothing was
    added is the set's judge."""

    max_chars: int = DEFAULT_EXERCISE_SIMPLE_HOWTO_MAX_CHARS

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        if not isinstance(ctx.expected_output, SimpleHowto):
            return {"kind": isinstance(ctx.output, NoHowto)}
        if not isinstance(ctx.output, SimpleHowto):
            return {"kind": False}
        text = ctx.output.text.strip()
        deps = SimpleHowtoDeps.model_validate(ctx.inputs.deps)
        return {
            "kind": True,
            "length": 0 < len(text) <= self.max_chars,
            "no_new_digit": not has_new_digit(text, deps),
        }
