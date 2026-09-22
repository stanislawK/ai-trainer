from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent, TemplateStr
from pydantic_ai.models.openrouter import OpenRouterModelSettings
from pydantic_ai.models.test import TestModel
from pydantic_handlebars import TemplateSchemaError

from ai_trainer.llm.prompts.template import (
    PromptTemplate,
    PromptTemplateVariableError,
    UnknownPromptTemplateError,
)

_PERSONA_ID = "_persona"


class PromptRegistry:
    """Loads persona and template bodies from `<root>/<id>/v<N>.<locale>.md` (ADR-0008)."""

    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._templates: dict[tuple[str, int], PromptTemplate[BaseModel, BaseModel]] = {}

    def register[DepsT: BaseModel, OutputT: BaseModel](
        self, template: PromptTemplate[DepsT, OutputT]
    ) -> None:
        self._templates[(template.id, template.version)] = template

    def get(self, template_id: str, version: int) -> PromptTemplate[BaseModel, BaseModel]:
        try:
            return self._templates[(template_id, version)]
        except KeyError:
            raise UnknownPromptTemplateError(template_id, version) from None

    def load_body(self, template: PromptTemplate[BaseModel, BaseModel]) -> str:
        return self._body_path(template.id, template.version, template.locale).read_text()

    def load_persona(self, *, version: int = 1, locale: str = "en") -> str:
        return self._body_path(_PERSONA_ID, version, locale).read_text()

    def _body_path(self, template_id: str, version: int, locale: str) -> Path:
        return self._root / template_id / f"v{version}.{locale}.md"

    def build_agent(
        self, template_id: str, version: int, *, persona_version: int = 1
    ) -> Agent[BaseModel, BaseModel]:
        """Composes persona + template into an `Agent` (ADR-0008).

        Built with a placeholder `TestModel`, the same convention `OpenRouterGateway` uses
        (`src/ai_trainer/llm/gateway.py`): a caller passes the real, `Settings`-resolved model
        explicitly to `run`/`run_sync` (`model=...`), and tests substitute `FunctionModel` via
        `agent.override()` rather than rebuilding the agent per model.
        """
        template = self.get(template_id, version)
        persona = self.load_persona(version=persona_version, locale=template.locale)
        body = self.load_body(template)

        try:
            body_template = TemplateStr(body, deps_type=template.deps_type)
        except TemplateSchemaError as exc:
            variable = exc.result.issues[0].field_path
            raise PromptTemplateVariableError(
                variable=variable,
                deps_type=template.deps_type,
                template_id=template_id,
                version=version,
            ) from exc

        return Agent(
            TestModel(),
            deps_type=template.deps_type,
            output_type=template.output_type,
            instructions=[persona, body_template],
            model_settings=OpenRouterModelSettings(openrouter_cache_instructions=True),
        )
