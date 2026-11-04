"""Staging PMC articles by ID (ADR-0036, #458), with in-memory sources and a fake PMC port."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.knowledge_staging import (
    Covered,
    KnowledgeStaging,
    NotFound,
    PmcStageRequest,
    Refused,
    Staged,
    Unavailable,
)
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeFileType,
    KnowledgeSource,
    LicenseId,
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
    PmcUnavailable,
    pmc_placeholder_hash,
)
from tests.knowledge_support import MemorySources
from tests.pmc_support import article

NOW = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
ADMIN = UUID(int=42)


class Clock:
    def now(self) -> datetime:
        return NOW


class FakePmc:
    """Implements `PmcMetadataPort` from a dict; records which IDs were fetched."""

    def __init__(self, answers: Mapping[str, PmcArticle | PmcNotFound | PmcUnavailable]) -> None:
        self.answers = dict(answers)
        self.fetched: list[str] = []

    async def fetch(self, pmcid: str) -> PmcArticle | PmcNotFound | PmcUnavailable:
        self.fetched.append(pmcid)
        return self.answers.get(pmcid, PmcNotFound())


def _article(pmcid: str, **overrides: object) -> PmcArticle:
    return PmcArticle.model_validate(article(pmcid, **overrides))


def _staging(pmc: FakePmc, sources: MemorySources | None = None) -> KnowledgeStaging:
    return KnowledgeStaging(
        sources=sources or MemorySources(), pmc=pmc, clock=Clock(), sports=["gym", "climbing"]
    )


def _ask(pmcid: str, **overrides: object) -> PmcStageRequest:
    fields: dict[str, object] = {
        "pmcid": pmcid,
        "evidence_tier": EvidenceTier.PEER_REVIEWED,
        "sports": ("gym",),
        "topics": ("Strength",),
    }
    fields.update(overrides)
    return PmcStageRequest(**fields)  # type: ignore[arg-type]


def _stored(pmcid: str | None, **overrides: object) -> KnowledgeSource:
    source_id = uuid4()
    fields: dict[str, object] = {
        "id": source_id,
        "title": "A paper uploaded as a PDF",
        "author": None,
        "source_type": SourceType.RESEARCH_PAPER,
        "evidence_tier": EvidenceTier.PEER_REVIEWED,
        "language": SourceLanguage.EN,
        "sports": ("gym",),
        "topics": (),
        "license_note": None,
        "license_id": LicenseId.CC_BY,
        "license_source": LicenseSource.ADMIN,
        "attribution": "x",
        "source_url": None,
        "pmcid": pmcid,
        "content_hash": f"hash-{source_id}",
        "file_type": KnowledgeFileType.PDF,
        "file_name": "paper.pdf",
        "size_bytes": 10,
        "storage_key": f"sources/{source_id}",
        "status": SourceStatus.INGESTED,
        "uploaded_by_user_id": ADMIN,
        "uploaded_at": NOW,
    }
    fields.update(overrides)
    return KnowledgeSource.model_validate(fields)


async def test_a_cc_by_article_is_staged_as_a_proposed_pmc_source() -> None:
    sources = MemorySources()
    staging = _staging(FakePmc({"PMC10": _article("PMC10")}), sources)

    [outcome] = await staging.stage_pmc(ADMIN, [_ask("PMC10", language=SourceLanguage.PL)])

    assert isinstance(outcome, Staged)
    source = sources.rows[outcome.source.id]
    assert source.status is SourceStatus.PROPOSED
    assert (source.title, source.pmcid, source.doi, source.pmid) == (
        "Article PMC10", "PMC10", "10.9999/test.10", "10",
    )  # fmt: skip
    assert (source.license_id, source.license_source) == (LicenseId.CC_BY, LicenseSource.PMC)
    assert (
        source.attribution == "Article PMC10. J Test. 2026;1:10. Licence: CC BY. Source: NLM PMC."
    )
    assert source.source_type is SourceType.RESEARCH_PAPER
    assert source.language is SourceLanguage.PL
    assert source.topics == ("strength",)
    assert source.source_url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC10/"
    assert source.file_type is KnowledgeFileType.JATS
    assert (source.storage_key, source.file_name, source.size_bytes) == (None, None, None)
    assert source.content_hash == pmc_placeholder_hash("PMC10")
    assert (source.uploaded_by_user_id, source.uploaded_at) == (ADMIN, NOW)


async def test_retracted_and_unadmitted_licences_are_refused_with_reasons() -> None:
    pmc = FakePmc(
        {
            "PMC1": _article("PMC1", is_retracted=True),
            "PMC2": _article("PMC2", license_code="TDM"),
            "PMC3": _article("PMC3", license_code=None),
            "PMC4": _article("PMC4", license_code="CC BY-NC-ND"),
        }
    )
    sources = MemorySources()

    outcomes = await _staging(pmc, sources).stage_pmc(ADMIN, [_ask(f"PMC{n}") for n in range(1, 5)])

    assert outcomes[:3] == [
        Refused("PMC1", PmcRefusal.RETRACTED, "CC BY"),
        Refused("PMC2", PmcRefusal.LICENCE_NOT_ADMITTED, "TDM"),
        Refused("PMC3", PmcRefusal.LICENCE_NOT_ADMITTED, None),
    ]
    assert isinstance(outcomes[3], Staged)
    assert outcomes[3].source.license_id is LicenseId.CC_BY_NC_ND
    assert [s.pmcid for s in sources.rows.values()] == ["PMC4"]


async def test_an_id_already_a_source_is_covered_without_a_fetch() -> None:
    sources = MemorySources()
    existing = _stored("PMC7")
    sources.rows[existing.id] = existing
    pmc = FakePmc({"PMC7": _article("PMC7")})

    assert await _staging(pmc, sources).stage_pmc(ADMIN, [_ask("PMC7")]) == [
        Covered("PMC7", existing)
    ]
    assert pmc.fetched == []


async def test_a_paper_uploaded_with_the_same_doi_is_covered() -> None:
    sources = MemorySources()
    uploaded = _stored(None, doi="10.9999/test.8")
    sources.rows[uploaded.id] = uploaded

    outcomes = await _staging(FakePmc({"PMC8": _article("PMC8")}), sources).stage_pmc(
        ADMIN, [_ask("PMC8")]
    )

    assert outcomes == [Covered("PMC8", uploaded)]
    assert list(sources.rows) == [uploaded.id]


async def test_a_racing_insert_is_covered() -> None:
    sources = MemorySources()
    racing = _stored("PMC9", status=SourceStatus.PROPOSED)
    sources.racing = racing

    outcomes = await _staging(FakePmc({"PMC9": _article("PMC9")}), sources).stage_pmc(
        ADMIN, [_ask("PMC9")]
    )

    assert outcomes == [Covered("PMC9", racing)]


class _VanishingSources(MemorySources):
    """The conflicting row is deleted between the skipped insert and the lookup after it."""

    async def add_if_new(self, source: NewKnowledgeSource) -> KnowledgeSource | None:
        return None


async def test_a_conflict_whose_row_is_gone_is_unavailable() -> None:
    staging = _staging(FakePmc({"PMC6": _article("PMC6")}), _VanishingSources())

    assert await staging.stage_pmc(ADMIN, [_ask("PMC6")]) == [
        Unavailable("PMC6", PmcFetchProblem.FAILED)
    ]


async def test_unknown_and_unreachable_ids_are_reported_per_id() -> None:
    pmc = FakePmc(
        {
            "PMC2": PmcUnavailable(PmcFetchProblem.TIMEOUT),
            "PMC3": _article("PMC3"),
        }
    )

    outcomes = await _staging(pmc).stage_pmc(ADMIN, [_ask("PMC1"), _ask("PMC2"), _ask("PMC3")])

    assert outcomes[:2] == [NotFound("PMC1"), Unavailable("PMC2", PmcFetchProblem.TIMEOUT)]
    assert isinstance(outcomes[2], Staged)


async def test_metadata_the_template_refuses_is_malformed_for_that_id_only() -> None:
    # Built past validation, as a future drift in PMC's fields could arrive: a PMID the
    # template's pattern refuses.
    bad = _article("PMC5").model_copy(update={"pmid": "not-a-pmid"})
    pmc = FakePmc({"PMC5": bad, "PMC4": _article("PMC4")})

    outcomes = await _staging(pmc).stage_pmc(ADMIN, [_ask("PMC5"), _ask("PMC4")])

    assert outcomes[0] == Unavailable("PMC5", PmcFetchProblem.MALFORMED)
    assert isinstance(outcomes[1], Staged)


class _BrokenSources(MemorySources):
    """The database fails for one article's lookup; the others' lookups take a moment."""

    async def find_by_identifiers(
        self, *, pmcid: str | None = None, doi: str | None = None, pmid: str | None = None
    ) -> KnowledgeSource | None:
        if pmcid == "PMC1":
            raise ConnectionError("database gone")
        await asyncio.sleep(0.01)
        return await super().find_by_identifiers(pmcid=pmcid, doi=doi, pmid=pmid)


async def test_an_unexpected_error_cancels_the_other_stages_before_it_propagates() -> None:
    sources = _BrokenSources()
    pmc = FakePmc({f"PMC{n}": _article(f"PMC{n}") for n in range(2, 6)})

    with pytest.raises(ExceptionGroup) as raised:
        await _staging(pmc, sources).stage_pmc(ADMIN, [_ask(f"PMC{n}") for n in range(1, 6)])

    assert raised.group_contains(ConnectionError)
    # Cancelled mid-lookup: nothing was staged behind the failed request's back.
    assert sources.rows == {}


@pytest.mark.parametrize(
    ("ask", "problem"),
    [
        (_ask("PMC1", sports=("chess",)), "Name only the app's sports: chess."),
        (_ask("PMC1", evidence_tier=EvidenceTier.CATALOGUE), "That tier is reserved"),
        (_ask("PMC1", evidence_tier=EvidenceTier.COACHING_BOOK), None),
    ],
    ids=["unknown-sport", "catalogue-tier", "valid"],
)
def test_a_request_is_checked_before_any_fetch(ask: PmcStageRequest, problem: str | None) -> None:
    found = _staging(FakePmc({})).problem([ask, _ask("PMC2")])
    if problem is None:
        assert found is None
    else:
        assert found is not None
        assert found.startswith(problem)


def test_a_repeated_pmc_id_is_refused_rather_than_one_entry_guessed() -> None:
    asks = [_ask("PMC5"), _ask("PMC4", sports=("climbing",)), _ask("PMC5"), _ask("PMC4")]

    assert _staging(FakePmc({})).problem(asks) == "Name each PMC ID once: PMC4, PMC5."
