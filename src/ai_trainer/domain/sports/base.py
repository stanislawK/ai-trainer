from typing import Literal

from pydantic import BaseModel, ConfigDict

# How an extraction found `occurred_on` (ADR-0014): typed as a date, resolved from a relative
# expression, or today because the message gave none.
type DateSource = Literal["stated", "relative", "assumed_today"]


class ExtractionSpec(BaseModel):
    """The extraction template a plugin names (ADR-0006): its ID and version under
    `llm/prompts/`, and the model its output is validated against."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    version: int
    output_type: type[BaseModel]


class SportPlugin(BaseModel):
    """What the app knows about a sport (ADR-0006): an ID, the key its label is looked up by
    in templates (G5), its Lucide icon and, once the sport can be logged, its extraction
    template. Load calculators join this as they land."""

    model_config = ConfigDict(frozen=True)

    id: str
    label_key: str
    icon: str
    extraction: ExtractionSpec | None = None
