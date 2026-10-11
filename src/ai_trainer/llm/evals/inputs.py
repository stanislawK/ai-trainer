from pydantic import BaseModel


class EvalCaseInputs(BaseModel):
    """A dataset case's input: the user-facing prompt plus the template's deps as a plain
    dict, so one `Dataset[EvalCaseInputs, ...]` shape works for every template (ADR-0009)
    without a per-template generated Inputs type."""

    prompt: str
    deps: dict[str, object] = {}
