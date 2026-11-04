"""Staging knowledge sources for an admin's approval (ADR-0036, PRD B7): papers by PMC ID (#458).

Every staged source is `proposed`, which the worker never reads (invariant 1). An article's
metadata comes from PMC's Cloud Service by its PMC ID alone (invariant 7). A retracted article
or one under a licence off the admit list is refused (invariant 4). Each ID gets its own
outcome, so one bad ID never fails the others.
"""

import asyncio
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import ValidationError

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.knowledge import KnowledgeSourcesRepositoryPort
from ai_trainer.application.ports.pmc import PmcMetadataPort
from ai_trainer.domain.knowledge import (
    UPLOAD_EVIDENCE_TIERS,
    EvidenceTier,
    KnowledgeFileType,
    KnowledgeSource,
    LicenseSource,
    NewKnowledgeSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
)
from ai_trainer.domain.pmc import (
    PmcArticle,
    PmcFetchProblem,
    PmcNotFound,
    PmcRefusal,
    pmc_attribution,
    pmc_licence,
    pmc_placeholder_hash,
)
from ai_trainer.domain.source_template import LicenceTerms, SourceTemplate

# Articles fetched at once: each is two small GETs to S3, and a request holds at most a few
# dozen IDs.
DEFAULT_CONCURRENCY = 5
_ARTICLE_URL = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"


@dataclass(frozen=True)
class PmcStageRequest:
    """One article to stage, with what the admin says about it."""

    pmcid: str
    evidence_tier: EvidenceTier
    sports: tuple[str, ...]
    topics: tuple[str, ...] = ()
    # PMC's metadata names no language; nearly every article is English.
    language: SourceLanguage = SourceLanguage.EN


@dataclass(frozen=True)
class Staged:
    pmcid: str
    source: KnowledgeSource


@dataclass(frozen=True)
class Covered:
    """A source with this PMC ID, DOI or PMID exists already, whatever its status."""

    pmcid: str
    source: KnowledgeSource


@dataclass(frozen=True)
class Refused:
    pmcid: str
    reason: PmcRefusal
    license_code: str | None


@dataclass(frozen=True)
class NotFound:
    pmcid: str


@dataclass(frozen=True)
class Unavailable:
    pmcid: str
    problem: PmcFetchProblem


type PmcOutcome = Staged | Covered | Refused | NotFound | Unavailable


class KnowledgeStaging:
    def __init__(
        self,
        *,
        sources: KnowledgeSourcesRepositoryPort,
        pmc: PmcMetadataPort,
        clock: ClockPort,
        sports: Collection[str],
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._sources = sources
        self._pmc = pmc
        self._clock = clock
        self._sports = sports
        self._concurrency = concurrency

    def problem(self, requests: Sequence[PmcStageRequest]) -> str | None:
        """What makes the request invalid as a whole, checked before any fetch: the admin's own
        fields, which no article's metadata can fix."""
        counts = Counter(r.pmcid for r in requests)
        repeated = sorted(pmcid for pmcid, count in counts.items() if count > 1)
        if repeated:
            # Two entries could disagree on tier, sports or topics; neither is guessed at.
            return f"Name each PMC ID once: {', '.join(repeated)}."
        unknown = sorted({s for r in requests for s in r.sports if s not in self._sports})
        if unknown:
            return f"Name only the app's sports: {', '.join(unknown)}."
        if any(r.evidence_tier not in UPLOAD_EVIDENCE_TIERS for r in requests):
            return "That tier is reserved for the catalogue."
        return None

    async def stage_pmc(
        self, admin_id: UUID, requests: Sequence[PmcStageRequest]
    ) -> list[PmcOutcome]:
        """One outcome per PMC ID, in the order asked. Check `problem` first. An unexpected error
        in one stage cancels the others before it propagates, so none goes on staging
        unreported."""
        limit = asyncio.Semaphore(self._concurrency)

        async def one(request: PmcStageRequest) -> PmcOutcome:
            async with limit:
                return await self._stage_one(admin_id, request)

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(request)) for request in requests]
        return [task.result() for task in tasks]

    async def _stage_one(self, admin_id: UUID, request: PmcStageRequest) -> PmcOutcome:
        pmcid = request.pmcid
        existing = await self._sources.find_by_identifiers(pmcid=pmcid)
        if existing is not None:
            return Covered(pmcid, existing)
        fetched = await self._pmc.fetch(pmcid)
        if isinstance(fetched, PmcNotFound):
            return NotFound(pmcid)
        if not isinstance(fetched, PmcArticle):
            return Unavailable(pmcid, fetched.problem)
        if fetched.is_retracted:
            return Refused(pmcid, PmcRefusal.RETRACTED, fetched.license_code)
        try:
            source = self._proposal(admin_id, request, fetched)
        except ValidationError:
            # Metadata the template refuses, which `problem` and `PmcArticle` didn't catch.
            return Unavailable(pmcid, PmcFetchProblem.MALFORMED)
        if source is None:
            return Refused(pmcid, PmcRefusal.LICENCE_NOT_ADMITTED, fetched.license_code)
        # The same paper may be a source already under its DOI or PMID, e.g. uploaded as a PDF.
        same_paper = await self._sources.find_by_identifiers(doi=source.doi, pmid=source.pmid)
        if same_paper is not None:
            return Covered(pmcid, same_paper)
        stored = await self._sources.add_if_new(source)
        if stored is not None:
            return Staged(pmcid, stored)
        # Another request staged it since the lookups.
        racing = await self._sources.find_by_identifiers(
            pmcid=pmcid, doi=source.doi, pmid=source.pmid
        )
        return (
            Unavailable(pmcid, PmcFetchProblem.FAILED) if racing is None else Covered(pmcid, racing)
        )

    def _proposal(
        self, admin_id: UUID, request: PmcStageRequest, article: PmcArticle
    ) -> NewKnowledgeSource | None:
        """#456's template filled from the metadata, as a `proposed` source; None when the
        licence isn't admitted."""
        license_id = pmc_licence(article.license_code)
        if license_id is None:
            return None
        template = SourceTemplate.model_validate(
            {
                "type": "article-journal",
                "title": article.title,
                "DOI": article.doi,
                "PMID": article.pmid,
                "PMCID": article.pmcid,
                "language": request.language,
                "URL": _ARTICLE_URL.format(pmcid=article.pmcid),
                "evidence_tier": request.evidence_tier,
                "source_type": SourceType.RESEARCH_PAPER,
                "sports": request.sports,
                "topics": request.topics,
                "licence": LicenceTerms(
                    license_id=license_id,
                    license_source=LicenseSource.PMC,
                    attribution=pmc_attribution(article),
                ),
            },
            context={"sports": self._sports},
        )
        licence = template.licence
        assert licence is not None
        return NewKnowledgeSource(
            **template.details().model_dump(),
            id=uuid4(),
            license_id=licence.license_id,
            license_source=licence.license_source,
            attribution=licence.attribution,
            status=SourceStatus.PROPOSED,
            # No bytes until #463 fetches the JATS (ADR-0036, amended at #458).
            content_hash=pmc_placeholder_hash(article.pmcid),
            file_type=KnowledgeFileType.JATS,
            file_name=None,
            size_bytes=None,
            storage_key=None,
            uploaded_by_user_id=admin_id,
            uploaded_at=self._clock.now(),
        )
