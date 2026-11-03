"""The one source template every way into the knowledge base validates (ADR-0036, #456).

A source is a CSL-JSON item, under CSL's own field names, plus the project's fields: evidence
tier, source type, sports, topics and a structured licence. The upload form, the staging routes
and `/stage-knowledge` all build one, so a source is checked by one set of rules whichever way
it came in. A template without a licence is one whose licence is still to be read from its
file (the #454 amendment).

Every refusal carries an error type the form can name per field: `missing`, `unknown` or
`not_a_url`, as Pydantic's own `missing` and `enum` do.
"""

from collections.abc import Collection
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from ai_trainer.domain.knowledge import (
    UPLOAD_EVIDENCE_TIERS,
    UPLOAD_SOURCE_TYPES,
    Doi,
    EvidenceTier,
    LicenseId,
    LicenseSource,
    PmcId,
    PubMedId,
    SourceDetails,
    SourceLanguage,
    SourceType,
    UploadProblem,
    web_url,
)

type _Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _refuse(problem: UploadProblem, message: str) -> PydanticCustomError:
    return PydanticCustomError(problem.value, message)


def _optional_text(value: object) -> object:
    if isinstance(value, str):
        return value.strip() or None
    return value


def _checked_url(value: str | None) -> str | None:
    if value is None:
        return None
    url = web_url(value)
    if isinstance(url, UploadProblem):
        raise _refuse(url, "Use a full link that starts with http:// or https://.")
    return url


class LicenceTerms(BaseModel):
    """ADR-0036's structured licence. `other` needs a note saying what permission the admin
    holds; a CC licence carries none, since its terms say it all."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    license_id: LicenseId
    license_source: LicenseSource
    license_url: str | None = None
    # The source's copyright statement, verbatim.
    copyright_statement: str | None = None
    # Shown with every answer that cites the source (ADR-0036 invariant 4).
    attribution: _Text
    license_note: str | None = Field(default=None, validate_default=True)

    @field_validator("license_url", "copyright_statement", "license_note", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("license_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _checked_url(value)

    @field_validator("license_note")
    @classmethod
    def _note_for_other(cls, value: str | None, info: ValidationInfo) -> str | None:
        license_id = info.data.get("license_id")
        if license_id is LicenseId.OTHER and value is None:
            raise _refuse(UploadProblem.MISSING, "Say what permission you hold.")
        return value if license_id is LicenseId.OTHER else None


class CslName(BaseModel):
    """A CSL name: a person's parts, or one literal string for anything else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str | None = None
    given: str | None = None
    literal: str | None = None

    @model_validator(mode="after")
    def _named(self) -> Self:
        if not any(part and part.strip() for part in (self.family, self.given, self.literal)):
            raise _refuse(UploadProblem.MISSING, "A name needs a family name or a literal.")
        return self

    def display(self) -> str:
        if self.literal:
            return self.literal.strip()
        return " ".join(p.strip() for p in (self.given, self.family) if p and p.strip())


class CslDate(BaseModel):
    """A CSL date: `date-parts` of year, month and day, the later two optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    date_parts: tuple[tuple[int] | tuple[int, int] | tuple[int, int, int], ...] = Field(
        alias="date-parts", min_length=1
    )


class SourceTemplate(BaseModel):
    """ADR-0036's template: a CSL-JSON item (`type`, `title`, `author`, `issued`, `DOI`, `PMID`,
    `PMCID`, `ISBN`, `abstract`, `keyword`, `language`, `URL`, `container-title`) plus the
    project's fields. `licence` is None when it is to be detected from the file.

    Validate with `model_validate(data, context={"sports": …})`: the sports a source may name
    are the registry's, which the domain doesn't hold."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    csl_type: str | None = Field(default=None, alias="type")
    title: _Text
    author: tuple[CslName, ...] = ()
    issued: CslDate | None = None
    doi: Doi | None = Field(default=None, alias="DOI")
    pmid: PubMedId | None = Field(default=None, alias="PMID")
    pmcid: PmcId | None = Field(default=None, alias="PMCID")
    isbn: str | None = Field(default=None, alias="ISBN")
    abstract: str | None = None
    keyword: str | None = None
    language: SourceLanguage
    url: str | None = Field(default=None, alias="URL")
    container_title: str | None = Field(default=None, alias="container-title")
    evidence_tier: EvidenceTier
    source_type: SourceType
    sports: tuple[str, ...] = Field(min_length=1)
    topics: tuple[str, ...] = ()
    licence: LicenceTerms | None

    @field_validator(
        "csl_type", "doi", "pmid", "pmcid", "isbn", "abstract", "keyword", "url",
        "container_title",
        mode="before",
    )  # fmt: skip
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _checked_url(value)

    @field_validator("evidence_tier")
    @classmethod
    def _offered_tier(cls, value: EvidenceTier) -> EvidenceTier:
        # The catalogue's tier is reserved for its own rows (#427).
        if value not in UPLOAD_EVIDENCE_TIERS:
            raise _refuse(UploadProblem.UNKNOWN, "That tier is reserved for the catalogue.")
        return value

    @field_validator("source_type")
    @classmethod
    def _offered_type(cls, value: SourceType) -> SourceType:
        if value not in UPLOAD_SOURCE_TYPES:
            raise _refuse(UploadProblem.UNKNOWN, "That type is reserved for the catalogue.")
        return value

    @field_validator("sports")
    @classmethod
    def _known_sports(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        # A caller that forgot the registry's sports is a bug, not a refused source.
        if not isinstance(info.context, dict) or "sports" not in info.context:
            raise RuntimeError("validate a SourceTemplate with context={'sports': …}")
        known: Collection[str] = info.context["sports"]
        if any(sport not in known for sport in value):
            raise _refuse(UploadProblem.UNKNOWN, "Name only the app's sports.")
        return tuple(dict.fromkeys(value))

    @field_validator("topics")
    @classmethod
    def _tidy_topics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t.strip().lower() for t in value if t.strip()))

    def details(self) -> SourceDetails:
        """The fields the manifest stores for every source."""
        author = ", ".join(name.display() for name in self.author)
        return SourceDetails(
            title=self.title,
            author=author or None,
            source_type=self.source_type,
            evidence_tier=self.evidence_tier,
            language=self.language,
            sports=self.sports,
            topics=self.topics,
            license_note=None if self.licence is None else self.licence.license_note,
            source_url=self.url,
            pmcid=self.pmcid,
            doi=self.doi,
            pmid=self.pmid,
            isbn=self.isbn,
        )
