from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ModelMessage, ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import (
    PromptTemplate,
    PromptTemplateVariableError,
    UnknownPromptTemplateError,
)

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "prompts"


class SampleDeps(BaseModel):
    name: str
    sport: str


class SampleOutput(BaseModel):
    reply: str


_SAMPLE_TEMPLATE = PromptTemplate(
    id="sample",
    version=1,
    locale="en",
    deps_type=SampleDeps,
    output_type=SampleOutput,
    model_settings_key="sample_template_model",
)

_BROKEN_TEMPLATE = PromptTemplate(
    id="broken",
    version=1,
    locale="en",
    deps_type=SampleDeps,
    output_type=SampleOutput,
    model_settings_key="sample_template_model",
)


def _registry() -> PromptRegistry:
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(_SAMPLE_TEMPLATE)
    registry.register(_BROKEN_TEMPLATE)
    return registry


def _reply_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart('{"reply": "hi Alex"}')])


def test_loads_persona_and_sample_template_by_id_and_version() -> None:
    registry = _registry()

    persona = registry.load_persona()
    template = registry.get("sample", 1)
    body = registry.load_body(template)

    assert "supportive training companion" in persona
    assert template.id == "sample"
    assert template.version == 1
    assert template.deps_type is SampleDeps
    assert template.output_type is SampleOutput
    assert "{{name}}" in body
    assert "{{sport}}" in body


def test_get_unknown_template_raises_typed_error() -> None:
    registry = _registry()

    with pytest.raises(UnknownPromptTemplateError) as exc_info:
        registry.get("does-not-exist", 1)

    assert "does-not-exist" in str(exc_info.value)


async def test_build_agent_orders_persona_before_template_instructions() -> None:
    registry = _registry()

    agent = registry.build_agent("sample", 1)

    assert isinstance(agent, Agent)
    with agent.override(model=FunctionModel(_reply_response)):
        result = await agent.run("hi", deps=SampleDeps(name="Alex", sport="climbing"))
    first_message = result.all_messages()[0]
    assert isinstance(first_message, ModelRequest)
    instructions = first_message.instructions
    assert instructions is not None
    persona_index = instructions.index("supportive training companion")
    template_index = instructions.index("Greet Alex, who trains climbing.")
    assert persona_index < template_index


def test_build_agent_fails_on_unknown_template_variable() -> None:
    registry = _registry()

    with pytest.raises(PromptTemplateVariableError) as exc_info:
        registry.build_agent("broken", 1)

    message = str(exc_info.value)
    assert "favorite_color" in message
    assert "SampleDeps" in message


def test_model_settings_key_resolves_the_configured_model_id() -> None:
    """The intended caller pattern (ADR-0007): resolve the model id from `Settings` by the
    attribute name `model_settings_key` carries, then pass the built `Model` to `run`/
    `run_sync` — not to `build_agent`, which stays `Settings`-agnostic."""

    class _SettingsStub:
        sample_template_model = "openai/gpt-5-mini"

    model_id = getattr(_SettingsStub(), _SAMPLE_TEMPLATE.model_settings_key)

    assert model_id == "openai/gpt-5-mini"
