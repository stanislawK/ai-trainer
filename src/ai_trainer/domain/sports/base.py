from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

# How an extraction found `occurred_on` (ADR-0014): typed as a date, resolved from a relative
# expression, or today because the message gave none.
type DateSource = Literal["stated", "relative", "assumed_today"]


class SessionExtraction(BaseModel):
    """What every `<sport>.extract` template returns (ADR-0006, ADR-0014): an absolute date and
    how it was found, plus the sport's own fields, which only its plugin turns into a payload."""

    occurred_on: date
    date_source: DateSource

    def to_payload(self) -> BaseModel:
        """The sport payload this extraction describes, at the plugin's current version."""
        raise NotImplementedError


class ExtractionSpec(BaseModel):
    """The extraction template a plugin names (ADR-0006): its ID and version under
    `llm/prompts/`, the model its output is validated against, and the payload version that
    output's `to_payload` writes (ADR-0022 invariant 4)."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    version: int
    output_type: type[SessionExtraction]
    payload_type: type[BaseModel]
    payload_version: int


class SportPlugin(BaseModel):
    """What the app knows about a sport (ADR-0006): an ID, the key its label is looked up by
    in templates (G5), its Lucide icon and, once the sport can be logged, its extraction
    template. Load calculators join this as they land."""

    model_config = ConfigDict(frozen=True)

    id: str
    label_key: str
    icon: str
    extraction: ExtractionSpec | None = None
