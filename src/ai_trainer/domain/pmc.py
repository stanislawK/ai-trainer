"""PMC's article metadata, as its Cloud Service bucket serves it (ADR-0036, #458).

`pmc-oa-opendata` keeps one JSON object per article version, `metadata/<PMCID>.<n>.json`, with
the article's identifiers, title, journal citation, retraction flag and licence code. Only a
CC licence is admitted; PMC's `TDM` code (author manuscripts for text mining), a missing code
or anything else is refused (ADR-0036 Licences).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, StringConstraints

from ai_trainer.domain.knowledge import LicenseId, PmcId, content_hash

# Users must "acknowledge NLM as the source of the data in a clear and conspicuous manner"
# (the bucket's README, read 2026-11-04).
PMC_ATTRIBUTION = "Source: NLM PMC."

_LICENCES: Mapping[str, LicenseId] = {
    "CC0": LicenseId.CC0,
    "CC BY": LicenseId.CC_BY,
    "CC BY-SA": LicenseId.CC_BY_SA,
    "CC BY-NC": LicenseId.CC_BY_NC,
    "CC BY-NC-SA": LicenseId.CC_BY_NC_SA,
    "CC BY-ND": LicenseId.CC_BY_ND,
    "CC BY-NC-ND": LicenseId.CC_BY_NC_ND,
}
_DOI = re.compile(r"^10\.[0-9.]+/.")
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")


class PmcRefusal(StrEnum):
    RETRACTED = "retracted"
    LICENCE_NOT_ADMITTED = "licence_not_admitted"


class PmcFetchProblem(StrEnum):
    TIMEOUT = "timeout"
    FAILED = "failed"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class PmcNotFound:
    """The bucket holds no version of that PMC ID."""


@dataclass(frozen=True)
class PmcUnavailable:
    problem: PmcFetchProblem


def _bare_doi(value: object) -> object:
    """A bare, lowercased DOI, or None for anything that isn't one: a bad DOI from PMC costs the
    source its DOI, never the source."""
    if not isinstance(value, str):
        return value
    doi = value.strip().lower()
    for prefix in _DOI_PREFIXES:
        doi = doi.removeprefix(prefix)
    return doi if _DOI.match(doi) else None


def _bare_pmid(value: object) -> str | None:
    """PMC writes the PMID as a number. Anything that isn't digits is dropped, as a bad DOI is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and value.strip().isdecimal() and value.strip().isascii():
        return value.strip()
    return None


class PmcArticle(BaseModel):
    """The fields staging reads from one version's JSON; the rest are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pmcid: PmcId
    version: int
    pmid: Annotated[str | None, BeforeValidator(_bare_pmid)] = None
    doi: Annotated[str | None, BeforeValidator(_bare_doi)] = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    citation: str | None = None
    is_retracted: bool
    license_code: str | None = None


def pmc_licence(code: str | None) -> LicenseId | None:
    """The admitted licence for PMC's `license_code`, or None when it is refused."""
    if code is None:
        return None
    spelled = re.sub(r"\s+", " ", code.strip().upper())
    return _LICENCES.get(re.sub(r"^CC-", "CC ", spelled))


def pmc_attribution(article: PmcArticle) -> str:
    """Title, journal citation and licence, then NLM as the source."""
    parts = [f"{article.title.rstrip('.')}."]
    if article.citation and article.citation.strip():
        parts.append(f"{article.citation.strip().rstrip('.')}.")
    parts.append(f"Licence: {article.license_code}.")
    parts.append(PMC_ATTRIBUTION)
    return " ".join(parts)


def pmc_placeholder_hash(pmcid: str) -> str:
    """A staged article's `content_hash` until its JATS is fetched (ADR-0036, amended at #458):
    unique per article, and never a file's, since no file is the bytes `pmc:<PMCID>`."""
    return content_hash(f"pmc:{pmcid}".encode())
