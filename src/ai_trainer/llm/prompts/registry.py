from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent, AgentRetries, TemplateStr
from pydantic_ai.models.openrouter import OpenRouterModelSettings
from pydantic_ai.models.test import TestModel
from pydantic_handlebars import TemplateSchemaError

from ai_trainer.llm.prompts.template import (
    PromptTemplate,
    PromptTemplateVariableError,
    TemplateOutput,
    UnknownPromptTemplateError,
)

_PERSONA_ID = "_persona"


class PromptRegistry:
    """Loads persona and template bodies from `<root>/<id>/v<N>.<locale>.md` (ADR-0008)."""

    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._templates: dict[tuple[str, int], PromptTemplate[BaseModel, TemplateOutput]] = {}

    def register[DepsT: BaseModel, OutputT: TemplateOutput](
        self, template: PromptTemplate[DepsT, OutputT]
    ) -> None:
        self._templates[(template.id, template.version)] = template

    def get(self, template_id: str, version: int) -> PromptTemplate[BaseModel, TemplateOutput]:
        try:
            return self._templates[(template_id, version)]
        except KeyError:
            raise UnknownPromptTemplateError(template_id, version) from None

    def load_body(self, template: PromptTemplate[BaseModel, TemplateOutput]) -> str:
        return self._body_path(template.id, template.version, template.locale).read_text()

    def load_persona(self, *, version: int = 1, locale: str = "en") -> str:
        return self._body_path(_PERSONA_ID, version, locale).read_text()

    def load_fixed_reply(self, reply_id: str, version: int, *, locale: str = "en") -> str:
        """A reply sent as written, with no model call: ADR-0008's "not yet" reply. It lives
        beside the templates, so a saved reply records its ID and version like any other."""
        return self._body_path(reply_id, version, locale).read_text().strip()

    def render_instructions(
        self, template_id: str, version: int, deps: BaseModel, *, persona_version: int = 1
    ) -> str:
        """The instructions `build_agent`'s agent sends the model, as one string: persona, then
        the template rendered with `deps`. The gateway takes plain instructions (ADR-0007), so
        the app sends the model exactly what the template's eval runs sent it (ADR-0009)."""
        template = self.get(template_id, version)
        if not isinstance(deps, template.deps_type):
            raise TypeError(
                f"Template {template_id!r} v{version} takes {template.deps_type.__name__}, "
                f"not {type(deps).__name__}"
            )
        persona = self.load_persona(version=persona_version, locale=template.locale)
        body = TemplateStr(self.load_body(template), deps_type=template.deps_type).render(deps)
        # Pydantic AI's own joining of an instructions list (`InstructionPart.join`).
        return "\n\n".join(part.strip() for part in (persona, body) if part.strip())

    def _body_path(self, template_id: str, version: int, locale: str) -> Path:
        return self._root / template_id / f"v{version}.{locale}.md"

    def build_agent(
        self, template_id: str, version: int, *, persona_version: int = 1
    ) -> Agent[BaseModel, TemplateOutput]:
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

        model_settings = OpenRouterModelSettings(openrouter_cache_instructions=True)
        if template.temperature is not None:
            model_settings["temperature"] = template.temperature

        return Agent(
            TestModel(),
            deps_type=template.deps_type,
            output_type=template.output_type,
            instructions=[persona, body_template],
            model_settings=model_settings,
            retries=_retries(template.output_retries),
        )


def _retries(output_retries: int | None) -> AgentRetries | None:
    return AgentRetries(output=output_retries) if output_retries is not None else None
