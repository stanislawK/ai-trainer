"""The upload sheet's answers, read through the source template (ADR-0033, ADR-0036, #456).

The form is one of ADR-0036's ways in, so it builds a `SourceTemplate` and maps the template's
refusals back onto its own fields, every problem at once, so the sheet marks each field in one
round trip. Its licence select offers Detect from the file first: then the template carries no
licence, and the source waits as `detecting` (the #454 amendment).
"""

from collections.abc import Collection, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from ai_trainer.domain.knowledge import LicenseSource, UploadProblem
from ai_trainer.domain.source_template import SourceTemplate

# The licence select's first option and default.
DETECT_LICENCE = "detect"


class UploadField(StrEnum):
    FILE = "file"
    TITLE = "title"
    SOURCE_TYPE = "source_type"
    EVIDENCE_TIER = "evidence_tier"
    LANGUAGE = "language"
    LICENSE_ID = "license_id"
    LICENSE_URL = "license_url"
    ATTRIBUTION = "attribution"
    LICENSE_NOTE = "license_note"
    SOURCE_URL = "source_url"
    SPORTS = "sports"


class UploadAnswers(BaseModel):
    """The form's fields as typed, kept so a refused form shows them again."""

    model_config = ConfigDict(frozen=True)

    title: str = ""
    author: str = ""
    source_type: str = ""
    evidence_tier: str = ""
    language: str = ""
    topics: str = ""
    # No default pick: a post without the field (a sheet loaded before the select existed) is
    # refused, never read as Detect. The fresh sheet still opens on Detect.
    license_id: str = ""
    license_url: str = ""
    copyright_statement: str = ""
    attribution: str = ""
    license_note: str = ""
    source_url: str = ""
    sports: tuple[str, ...] = ()

    @property
    def detects_licence(self) -> bool:
        return self.license_id.strip() == DETECT_LICENCE


# Where each of the template's fields sits on the form.
_FIELDS: Mapping[tuple[str | int, ...], UploadField] = {
    ("title",): UploadField.TITLE,
    ("source_type",): UploadField.SOURCE_TYPE,
    ("evidence_tier",): UploadField.EVIDENCE_TIER,
    ("language",): UploadField.LANGUAGE,
    ("URL",): UploadField.SOURCE_URL,
    ("sports",): UploadField.SPORTS,
    ("licence", "license_id"): UploadField.LICENSE_ID,
    ("licence", "license_url"): UploadField.LICENSE_URL,
    ("licence", "attribution"): UploadField.ATTRIBUTION,
    ("licence", "license_note"): UploadField.LICENSE_NOTE,
}
_PROBLEMS: Mapping[str, UploadProblem] = {
    "missing": UploadProblem.MISSING,
    "string_too_short": UploadProblem.MISSING,
    "too_short": UploadProblem.MISSING,
    "not_a_url": UploadProblem.NOT_A_URL,
}


def _present(**values: str) -> dict[str, str]:
    """A blank answer is no answer, so the template calls it missing, not malformed."""
    return {name: value.strip() for name, value in values.items() if value.strip()}


def _template_input(answers: UploadAnswers) -> dict[str, object]:
    licence: dict[str, str] | None = None
    if not answers.detects_licence:
        licence = {
            "license_source": LicenseSource.ADMIN.value,
            **_present(
                license_id=answers.license_id,
                license_url=answers.license_url,
                copyright_statement=answers.copyright_statement,
                attribution=answers.attribution,
                license_note=answers.license_note,
            ),
        }
    author = answers.author.strip()
    return {
        **_present(
            title=answers.title,
            source_type=answers.source_type,
            evidence_tier=answers.evidence_tier,
            language=answers.language,
            URL=answers.source_url,
        ),
        "author": [{"literal": author}] if author else [],
        "sports": answers.sports,
        "topics": answers.topics.split(","),
        "licence": licence,
    }


def read_details(
    answers: UploadAnswers, *, known_sports: Collection[str]
) -> SourceTemplate | dict[UploadField, UploadProblem]:
    """The answers as a template, or every problem at once."""
    try:
        return SourceTemplate.model_validate(
            _template_input(answers), context={"sports": known_sports}
        )
    except ValidationError as error:
        problems: dict[UploadField, UploadProblem] = {}
        for detail in error.errors():
            # The form posts nothing else, so any other location is a bug: a KeyError, loudly.
            field = _FIELDS[tuple(detail["loc"])]
            problems.setdefault(field, _PROBLEMS.get(detail["type"], UploadProblem.UNKNOWN))
        return problems
