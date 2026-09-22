from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class PromptTemplate[DepsT: BaseModel, OutputT: BaseModel]:
    """A registered prompt: a body file, its typed variables and its typed output (ADR-0008)."""

    id: str
    version: int
    locale: str
    deps_type: type[DepsT]
    output_type: type[OutputT]
    # The attribute on `Settings` holding this template's OpenRouter model id — a caller
    # resolves it with `getattr(settings, template.model_settings_key)` before building the
    # `Model` it passes to the agent's `run`/`run_sync` (ADR-0007).
    model_settings_key: str


class UnknownPromptTemplateError(LookupError):
    """Raised by `PromptRegistry.get` for an id/version that was never registered."""

    def __init__(self, template_id: str, version: int) -> None:
        super().__init__(f"No prompt template registered for id={template_id!r}, version={version}")
        self.template_id = template_id
        self.version = version


class PromptTemplateVariableError(ValueError):
    """Raised when a template body references a variable absent from its deps model."""

    def __init__(
        self, *, variable: str, deps_type: type[BaseModel], template_id: str, version: int
    ) -> None:
        super().__init__(
            f"Template {template_id!r} v{version} references {{{{{variable}}}}}, "
            f"which is not a field of {deps_type.__name__}"
        )
        self.variable = variable
        self.deps_type = deps_type
