from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, JsonValue

from ai_trainer.domain.clarification import ChoiceOption

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

    def answered(self, answer: ChoiceOption) -> Self:
        """This extraction as the option the athlete picked on its sport's question reads it
        (ticket #164), enforced in code rather than trusted to the model; unchanged for a sport
        that never asks."""
        return self


class ExtractionQuestion(BaseModel):
    """What a `<sport>.extract` template returns in place of a draft when it can't read the log
    safely (B17, ADR-0015, ticket #164): the readings it weighs, likeliest first, which only its
    plugin turns into options."""

    def options(self) -> tuple[ChoiceOption, ...]:
        """One option per reading, likeliest first, each with a copy key and the values that
        tell the options apart."""
        raise NotImplementedError


class QuestionSpec(BaseModel):
    """The question a plugin's extraction may return instead of a draft, and the first
    template version that knows how to ask it (earlier ones stay registered for evals)."""

    model_config = ConfigDict(frozen=True)

    type: type[ExtractionQuestion]
    since_version: int


# The sport's own context from the athlete's recent sessions, most recent first, read from
# their stored payloads for the extraction's deps (ticket #164): what climbing kinds they do.
type RecentContext = Callable[[Sequence[Mapping[str, JsonValue]]], tuple[str, ...]]


# The athlete's edit of a draft's payload (F2): the stored payload and the edit form's sport
# fields in, the payload at the plugin's current version out. Raises `ValidationError` located
# at the payload's own fields.
type PayloadEdit = Callable[[Mapping[str, JsonValue], Mapping[str, JsonValue]], BaseModel]


class ExtractionSpec(BaseModel):
    """The extraction template a plugin names (ADR-0006): its ID and version under
    `llm/prompts/`, the model its output is validated against, the payload version that
    output's `to_payload` writes (ADR-0022 invariant 4), and how the athlete's edit of a draft
    applies to that payload. A plugin may also name the question its template can return
    instead of a draft, and the context it reads from the athlete's recent sessions (ticket
    #164)."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    version: int
    output_type: type[SessionExtraction]
    payload_type: type[BaseModel]
    payload_version: int
    apply_edit: PayloadEdit
    question: QuestionSpec | None = None
    recent: RecentContext | None = None


class SportPlugin(BaseModel):
    """What the app knows about a sport (ADR-0006): an ID, the key its label is looked up by
    in templates (G5), its Lucide icon and, once the sport can be logged, its extraction
    template. Load calculators join this as they land."""

    model_config = ConfigDict(frozen=True)

    id: str
    label_key: str
    icon: str
    extraction: ExtractionSpec | None = None


# Every sport payload carries its activities under this key (ADR-0006, ADR-0022).
_ACTIVITIES = "activities"


def extend_payload(
    payload_type: type[BaseModel],
    session: Mapping[str, JsonValue],
    addition: Mapping[str, JsonValue],
) -> BaseModel:
    """The session's payload with the addition's activities after its own (B16, ticket #165),
    read and validated by the sport's payload model. Raises `ValidationError` when either
    payload, or the result, doesn't read."""
    own = payload_type.model_validate(session).model_dump(mode="json")
    added = payload_type.model_validate(addition).model_dump(mode="json")
    return payload_type.model_validate(
        {**own, _ACTIVITIES: [*own[_ACTIVITIES], *added[_ACTIVITIES]]}
    )


def withdraw_payload(
    payload_type: type[BaseModel],
    session: Mapping[str, JsonValue],
    addition: Mapping[str, JsonValue],
) -> BaseModel:
    """The session's payload without the addition's activities, each removed once, the latest
    match first (B4 for a log added to a session, ticket #165). An activity the session no
    longer has is skipped. Raises `ValidationError` when the result doesn't read, e.g. when
    nothing is left."""
    own = payload_type.model_validate(session).model_dump(mode="json")
    added = payload_type.model_validate(addition).model_dump(mode="json")
    kept = list(own[_ACTIVITIES])
    for activity in added[_ACTIVITIES]:
        for index in range(len(kept) - 1, -1, -1):
            if kept[index] == activity:
                del kept[index]
                break
    return payload_type.model_validate({**own, _ACTIVITIES: kept})
