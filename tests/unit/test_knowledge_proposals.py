"""Deciding on proposals and setting a proposal's licence (ADR-0036 and its #454 amendment, #461),
on the in-memory sources."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.knowledge_sources import (
    KnowledgeSources,
    LicenceRefused,
    LicenceSet,
    NotProposed,
    ProposalsDecided,
    Skipped,
    SourceNotFound,
)
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    ProposalDecision,
    SkipReason,
    SourceLanguage,
    SourceStatus,
    SourceType,
    UploadProblem,
    content_hash,
)
from ai_trainer.domain.knowledge_form import UploadAnswers, UploadField
from tests.knowledge_support import MemorySources, MemoryStorage

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
ADMIN = UUID(int=7)


class Clock:
    def now(self) -> datetime:
        return NOW


def _source(
    title: str,
    *,
    status: SourceStatus = SourceStatus.PROPOSED,
    licensed: bool = True,
) -> KnowledgeSource:
    source_id = uuid4()
    return KnowledgeSource(
        id=source_id,
        title=title,
        author=None,
        source_type=SourceType.RESEARCH_PAPER,
        evidence_tier=EvidenceTier.PEER_REVIEWED,
        language=SourceLanguage.EN,
        sports=("gym",),
        topics=(),
        license_note=None,
        license_id=LicenseId.CC_BY if licensed else None,
        license_source=LicenseSource.PMC if licensed else None,
        attribution=f"{title}, CC BY" if licensed else None,
        source_url=None,
        content_hash=content_hash(source_id.bytes),
        file_type=None,
        file_name=None,
        size_bytes=None,
        storage_key=None,
        status=status,
        uploaded_by_user_id=None,
        uploaded_at=NOW,
    )


def _service(*sources: KnowledgeSource) -> tuple[KnowledgeSources, MemorySources]:
    memory = MemorySources()
    for source in sources:
        memory.rows[source.id] = source
    service = KnowledgeSources(
        sources=memory, storage=MemoryStorage(), clock=Clock(), max_upload_bytes=1, sports=["gym"]
    )
    return service, memory


async def test_approve_queues_licensed_proposals_as_the_admin() -> None:
    first, second = _source("First"), _source("Second")
    service, memory = _service(first, second)

    outcome = await service.decide(ADMIN, [first.id, second.id], ProposalDecision.APPROVE)

    assert outcome == ProposalsDecided(decision=ProposalDecision.APPROVE, done=2, skipped=())
    for source in (first, second):
        assert memory.rows[source.id].status is SourceStatus.UPLOADED
        assert memory.rows[source.id].uploaded_by_user_id == ADMIN


async def test_reject_sets_rejected() -> None:
    source = _source("Rejected")
    service, memory = _service(source)

    outcome = await service.decide(ADMIN, [source.id], ProposalDecision.REJECT)

    assert outcome.done == 1
    assert memory.rows[source.id].status is SourceStatus.REJECTED


async def test_skipped_sources_say_why() -> None:
    moved = _source("Moved", status=SourceStatus.UPLOADED)
    unlicensed = _source("Unlicensed", licensed=False)
    gone = uuid4()
    service, memory = _service(moved, unlicensed)

    outcome = await service.decide(ADMIN, [moved.id, unlicensed.id, gone], ProposalDecision.APPROVE)

    assert outcome == ProposalsDecided(
        decision=ProposalDecision.APPROVE,
        done=0,
        skipped=(
            Skipped(reason=SkipReason.MOVED, source=moved),
            Skipped(reason=SkipReason.UNLICENSED, source=unlicensed),
            Skipped(reason=SkipReason.GONE, source=None),
        ),
    )
    assert memory.rows[unlicensed.id].status is SourceStatus.PROPOSED


async def test_a_repeated_id_is_decided_once() -> None:
    source = _source("Twice")
    service, _ = _service(source)

    outcome = await service.decide(ADMIN, [source.id, source.id], ProposalDecision.APPROVE)

    assert (outcome.done, outcome.skipped) == (1, ())


async def test_no_ids_decides_nothing() -> None:
    service, _ = _service()

    outcome = await service.decide(ADMIN, [], ProposalDecision.REJECT)

    assert outcome == ProposalsDecided(decision=ProposalDecision.REJECT, done=0, skipped=())


def _licence(**fields: str) -> UploadAnswers:
    return UploadAnswers.model_validate(fields)


async def test_set_licence_records_the_admins_licence_on_a_proposal() -> None:
    source = _source("Roundtable", licensed=False)
    service, memory = _service(source)

    outcome = await service.set_licence(
        source.id,
        _licence(
            license_id="cc-by-sa",
            license_url="https://creativecommons.org/licenses/by-sa/4.0/",
            copyright_statement="© 2025 R. Lind",
            attribution="R. Lind, Roundtable, CC BY-SA 4.0",
        ),
    )

    assert isinstance(outcome, LicenceSet)
    stored = memory.rows[source.id]
    assert (stored.status, stored.license_id, stored.license_source) == (
        SourceStatus.PROPOSED,
        LicenseId.CC_BY_SA,
        LicenseSource.ADMIN,
    )
    assert stored.license_url == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert stored.copyright_statement == "© 2025 R. Lind"
    assert stored.attribution == "R. Lind, Roundtable, CC BY-SA 4.0"
    assert stored.license_note is None


@pytest.mark.parametrize(
    ("fields", "problems"),
    [
        ({"license_id": "", "attribution": "x"}, {UploadField.LICENSE_ID: UploadProblem.MISSING}),
        (
            {"license_id": "detect", "attribution": "x"},
            {UploadField.LICENSE_ID: UploadProblem.UNKNOWN},
        ),
        (
            {"license_id": "tdm", "attribution": "x"},
            {UploadField.LICENSE_ID: UploadProblem.UNKNOWN},
        ),
        ({"license_id": "cc0"}, {UploadField.ATTRIBUTION: UploadProblem.MISSING}),
        (
            {"license_id": "other", "attribution": "x"},
            {UploadField.LICENSE_NOTE: UploadProblem.MISSING},
        ),
        (
            {"license_id": "cc0", "attribution": "x", "license_url": "not a url"},
            {UploadField.LICENSE_URL: UploadProblem.NOT_A_URL},
        ),
    ],
    ids=["blank", "detect", "unknown", "no-attribution", "other-no-note", "bad-url"],
)
async def test_set_licence_refuses_each_problem_and_changes_nothing(
    fields: dict[str, str], problems: dict[UploadField, UploadProblem]
) -> None:
    source = _source("Roundtable", licensed=False)
    service, memory = _service(source)

    outcome = await service.set_licence(source.id, _licence(**fields))

    assert outcome == LicenceRefused(problems=problems)
    assert memory.rows[source.id] == source


async def test_set_licence_on_a_source_no_longer_proposed_is_refused() -> None:
    source = _source("Approved", status=SourceStatus.UPLOADED)
    service, memory = _service(source)

    outcome = await service.set_licence(source.id, _licence(license_id="cc0", attribution="x"))

    assert outcome == NotProposed(source=source)
    assert memory.rows[source.id] == source


async def test_set_licence_on_an_unknown_source_is_not_found() -> None:
    service, _ = _service()

    outcome = await service.set_licence(uuid4(), _licence(license_id="cc0", attribution="x"))

    assert outcome == SourceNotFound()


async def test_set_licence_that_loses_a_race_reports_the_new_status() -> None:
    source = _source("Raced", licensed=False)
    service, memory = _service(source)
    approved = source.model_copy(
        update={
            "status": SourceStatus.REJECTED,
            "license_id": LicenseId.CC0,
            "license_source": LicenseSource.ADMIN,
        }
    )
    memory.race_licence = approved

    outcome = await service.set_licence(source.id, _licence(license_id="cc0", attribution="x"))

    assert outcome == NotProposed(source=approved)


async def test_set_licence_that_loses_a_race_to_a_delete_is_not_found() -> None:
    source = _source("Raced away", licensed=False)
    service, memory = _service(source)
    memory.race_licence = "deleted"

    outcome = await service.set_licence(source.id, _licence(license_id="cc0", attribution="x"))

    assert outcome == SourceNotFound()
