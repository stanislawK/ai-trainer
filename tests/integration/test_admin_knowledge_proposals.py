"""Proposals on the admin's knowledge page through the real middleware stack and PostgreSQL
(ADR-0036, #461): bulk approve and reject, skipped sources reported, Set licence, the filter
chips, and the admin check before anything from the request is read.

In the serialized `db_schema` group: AC 2 asks the worker's queue for a claim, and a claim takes
the oldest waiting source, which could be a row another module committed."""

import re
from collections.abc import Callable
from datetime import timedelta
from html import unescape
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_chunk_repository import SqlAlchemyKnowledgeIngestionRepository
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
    content_hash,
)
from tests.integration.test_admin_knowledge import (
    FROZEN_NOW,
    Page,
    _page,
    _set_status,
    _source_html,
)

APPROVE = "/admin/knowledge/proposals/approve"
REJECT = "/admin/knowledge/proposals/reject"


def _pmcid() -> str:
    return f"PMC{uuid4().int % 10**12}"


async def _proposal(
    page: Page,
    title: str,
    *,
    license_id: LicenseId | None = LicenseId.CC_BY,
    license_source: LicenseSource | None = LicenseSource.PMC,
    status: SourceStatus = SourceStatus.PROPOSED,
    with_file: bool = False,
    **extra: object,
) -> KnowledgeSource:
    source_id = uuid4()
    pmcid = None if with_file else _pmcid()
    return await page.sources.add(
        NewKnowledgeSource(
            id=source_id,
            title=title,
            author="B. Schoenfeld et al.",
            source_type=SourceType.RESEARCH_PAPER,
            evidence_tier=EvidenceTier.PEER_REVIEWED,
            language=SourceLanguage.EN,
            sports=("gym",),
            topics=("hypertrophy",),
            license_note=None,
            license_id=license_id,
            license_source=license_source,
            attribution=None if license_id is None else f"{title}, CC BY",
            source_url=None,
            pmcid=pmcid,
            content_hash=content_hash(f"pmc:{pmcid or source_id}".encode()),
            file_type=KnowledgeFileType.PDF if with_file else None,
            file_name="file.pdf" if with_file else None,
            size_bytes=10 if with_file else None,
            storage_key=f"sources/{source_id}" if with_file else None,
            status=status,
            uploaded_by_user_id=None,
            uploaded_at=FROZEN_NOW,
            **extra,  # type: ignore[arg-type]  # the test passes only model fields
        )
    )


def _decide(page: Page, path: str, ids: list[UUID | str]) -> httpx2.Response:
    return page.client.post(path, data={"source_id": [str(i) for i in ids]}, headers=page.headers())


def _toast(html: str) -> str:
    found = re.search(r"<div[^>]*data-knowledge-toast[^>]*>(.*?)</div>", html, re.S)
    assert found, html
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", found.group(1))).split())


async def _status(page: Page, source_id: UUID) -> SourceStatus | None:
    source = await page.sources.get(source_id)
    return None if source is None else source.status


# --- AC 1: approving three proposals sets all three `uploaded`, as the approving admin --------


async def test_approving_three_proposals_queues_them_as_the_approving_admin(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-ac1")
    three = [await _proposal(page, f"AC1 paper {n}") for n in range(3)]
    left = await _proposal(page, "AC1 left alone")

    response = _decide(page, APPROVE, [s.id for s in three])

    assert response.status_code == 200, response.text
    for source in three:
        stored = await page.sources.get(source.id)
        assert stored is not None
        assert stored.status is SourceStatus.UPLOADED
        assert stored.uploaded_by_user_id == page.admin.id
    assert await _status(page, left.id) is SourceStatus.PROPOSED
    assert _toast(response.text).startswith("3 approved and queued for ingest.")


async def test_the_phone_and_desktop_twins_of_a_checkbox_count_once(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-twins")
    source = await _proposal(page, "Twin paper")

    response = _decide(page, APPROVE, [source.id, source.id])

    assert _toast(response.text) == "1 approved and queued for ingest."


# --- AC 2: rejecting one sets `rejected`, and the worker never reads it -----------------------


async def test_a_rejected_proposal_is_never_claimed_by_the_worker(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-ac2")
    source = await _proposal(page, "AC2 rejected paper", with_file=True)

    response = _decide(page, REJECT, [source.id])

    assert response.status_code == 200, response.text
    assert await _status(page, source.id) is SourceStatus.REJECTED
    assert _toast(response.text) == "1 rejected."
    queue = SqlAlchemyKnowledgeIngestionRepository(db_session_factory)
    claimed = await queue.claim_next(
        claim_id=uuid4(), now=FROZEN_NOW, lease_until=FROZEN_NOW + timedelta(minutes=5)
    )
    assert claimed is None or claimed.id != source.id
    assert await _status(page, source.id) is SourceStatus.REJECTED


# --- AC 3: a source no longer `proposed` is skipped and reported -----------------------------


async def test_a_source_approved_in_another_tab_is_skipped_and_named(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-ac3")
    fresh = await _proposal(page, "Deload practices in strength sports")
    other_tab = await _proposal(page, "Resistance training volume and hypertrophy")
    _decide(page, APPROVE, [other_tab.id])

    response = _decide(page, APPROVE, [fresh.id, other_tab.id])

    assert response.status_code == 200, response.text
    assert await _status(page, fresh.id) is SourceStatus.UPLOADED
    assert _toast(response.text) == (
        "1 approved and queued for ingest. 1 skipped: “Resistance training volume and"
        " hypertrophy” was no longer proposed. It was approved in another tab."
    )


async def test_rejected_deleted_and_unlicensed_sources_are_skipped_with_their_reasons(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-ac3-why")
    rejected = await _proposal(page, "Squat depth and knee load")
    await _set_status(db_session_factory, rejected.id, "rejected")
    unlicensed = await _proposal(
        page, "Coaches' roundtable on fatigue", license_id=None, license_source=None, with_file=True
    )
    deleted = await _proposal(page, "Gone paper")
    await page.sources.delete(deleted.id)

    response = _decide(page, APPROVE, [rejected.id, unlicensed.id, deleted.id])

    toast = _toast(response.text)
    assert toast.startswith("Nothing was approved. 3 skipped:")
    assert (
        "“Squat depth and knee load” was no longer proposed. It was rejected in another tab."
        in (toast)
    )
    assert "“Coaches' roundtable on fatigue” has no licence yet. Set one, then approve it." in (
        toast
    )
    assert "1 was deleted in another tab." in toast
    # ADR-0036's #454 amendment: the server refuses to approve a source with no licence.
    assert await _status(page, unlicensed.id) is SourceStatus.PROPOSED


# --- AC 4: a non-admin gets 403 before any source ID is parsed --------------------------------


@pytest.mark.parametrize(
    "path",
    [
        APPROVE,
        REJECT,
        "/admin/knowledge/sources/not-a-uuid/licence",
        "/admin/knowledge/sources/{id}/licence",
    ],
    ids=["approve", "reject", "licence-bad-id", "licence"],
)
async def test_a_non_admin_gets_403_on_the_proposal_routes_before_any_id_is_read(
    db_session_factory: Callable[[], AsyncSession], path: str
) -> None:
    owner = await _page(db_session_factory, f"proposals-ac4-admin-{len(path)}-{path[-6:]}")
    kept = await _proposal(owner, "AC4 kept", license_id=None, license_source=None)
    page = await _page(
        db_session_factory, f"proposals-ac4-user-{len(path)}-{path[-6:]}", admin=False
    )

    response = page.client.post(
        path.format(id=kept.id),
        data={"source_id": ["not-a-uuid", str(kept.id)], "license_id": "cc-by", "attribution": "x"},
        headers=page.headers(),
    )

    assert response.status_code == 403
    still = await owner.sources.get(kept.id)
    assert still is not None
    assert (still.status, still.license_id) == (SourceStatus.PROPOSED, None)


async def test_a_malformed_source_id_from_an_admin_is_404_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-bad-id")
    source = await _proposal(page, "Bad id paper")

    response = _decide(page, APPROVE, [source.id, "not-a-uuid"])

    assert response.status_code == 404
    assert await _status(page, source.id) is SourceStatus.PROPOSED


async def test_a_decision_without_a_csrf_token_is_refused(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-csrf")
    source = await _proposal(page, "CSRF paper")

    response = page.client.post(
        APPROVE, data={"source_id": [str(source.id)]}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 403
    assert await _status(page, source.id) is SourceStatus.PROPOSED


# --- Set licence (ADR-0036's #454 amendment) ---------------------------------------------------


def _set_licence(page: Page, source_id: UUID | str, **fields: str) -> httpx2.Response:
    return page.client.post(
        f"/admin/knowledge/sources/{source_id}/licence", data=fields, headers=page.headers()
    )


async def test_set_licence_lets_an_unlicensed_proposal_be_approved(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-set")
    source = await _proposal(
        page, "Roundtable", license_id=None, license_source=None, with_file=True
    )

    response = _set_licence(
        page,
        source.id,
        license_id="other",
        license_note="R. Lind's written permission, email of 28 Oct 2026.",
        copyright_statement="© 2025 R. Lind",
        attribution="R. Lind, Roundtable, used with permission",
    )

    assert response.status_code == 200, response.text
    stored = await page.sources.get(source.id)
    assert stored is not None
    assert (stored.status, stored.license_id, stored.license_source) == (
        SourceStatus.PROPOSED,
        LicenseId.OTHER,
        LicenseSource.ADMIN,
    )
    assert stored.license_note == "R. Lind's written permission, email of 28 Oct 2026."
    assert stored.copyright_statement == "© 2025 R. Lind"
    assert stored.attribution == "R. Lind, Roundtable, used with permission"
    _decide(page, APPROVE, [source.id])
    assert await _status(page, source.id) is SourceStatus.UPLOADED


async def test_set_licence_other_without_a_note_reopens_the_sheet_marked(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-set-note")
    source = await _proposal(page, "Roundtable", license_id=None, license_source=None)

    response = _set_licence(page, source.id, license_id="other", attribution="R. Lind")

    assert response.status_code == 422
    sheet = re.search(
        rf'<form[^>]*data-licence-sheet="{source.id}"[^>]*>.*?</form>', response.text, re.S
    )
    assert sheet, response.text
    assert 'data-open="true"' in sheet.group(0)
    assert re.search(r'name="license_note"[^>]*aria-invalid="true"', sheet.group(0))
    assert 'value="R. Lind"' in sheet.group(0)
    stored = await page.sources.get(source.id)
    assert stored is not None
    assert stored.license_id is None


async def test_set_licence_refuses_detect(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-set-detect")
    source = await _proposal(page, "Roundtable", license_id=None, license_source=None)

    response = _set_licence(page, source.id, license_id="detect", attribution="R. Lind")

    assert response.status_code == 422
    assert re.search(r'name="license_id"[^>]*aria-invalid="true"', response.text)


async def test_set_licence_on_a_source_no_longer_proposed_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-set-moved")
    source = await _proposal(page, "Moved paper")
    _decide(page, APPROVE, [source.id])

    response = _set_licence(page, source.id, license_id="cc0", attribution="Someone")

    assert response.status_code == 409
    assert "no longer proposed" in unescape(response.text)
    stored = await page.sources.get(source.id)
    assert stored is not None
    assert stored.license_id is LicenseId.CC_BY


async def test_set_licence_on_a_proposal_that_has_one_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-set-licensed")
    source = await _proposal(page, "PMC licensed paper")

    response = _set_licence(page, source.id, license_id="cc0", attribution="Someone")

    assert response.status_code == 409
    assert "already has a licence" in unescape(response.text)
    stored = await page.sources.get(source.id)
    assert stored is not None
    assert (stored.license_id, stored.license_source) == (LicenseId.CC_BY, LicenseSource.PMC)


@pytest.mark.parametrize("source_id", ["unknown", "not-a-uuid"], ids=["unknown", "malformed"])
async def test_set_licence_on_an_unknown_or_malformed_source_is_404(
    db_session_factory: Callable[[], AsyncSession], source_id: str
) -> None:
    page = await _page(db_session_factory, f"proposals-set-{source_id}")
    path_id = uuid4() if source_id == "unknown" else source_id

    response = _set_licence(page, path_id, license_id="cc0", attribution="Someone")

    assert response.status_code == 404


# --- The list: chips, select column, identifiers, the detected line, the reason --------------


async def test_the_proposed_chip_lists_only_proposals_with_counts(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-chips")
    proposal = await _proposal(page, "Chip proposal")
    ingested = await _proposal(page, "Chip ingested")
    await _set_status(db_session_factory, ingested.id, "ingested")

    html = page.client.get("/admin/knowledge?show=proposed").text

    assert f'data-source-id="{proposal.id}"' in html
    assert f'data-source-id="{ingested.id}"' not in html
    chips = re.findall(
        r'data-knowledge-chip="(\w+)"[^>]*aria-pressed="(true|false)"[^>]*>.*?([^<>]+)</button>',
        unescape(html),
    )
    # Counts aren't asserted exactly: another module may have committed sources.
    assert [(chip, on) for chip, on, _ in chips] == [
        ("all", "false"),
        ("proposed", "true"),
        ("ingested", "false"),
        ("rejected", "false"),
    ]
    labels = [label.strip() for _, _, label in chips]
    assert [re.sub(r"\d+", "N", label) for label in labels] == [
        "All · N",
        "Proposed · N",
        "Ingested · N",
        "Rejected · N",
    ]


async def test_an_unknown_filter_shows_everything(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-chips-unknown")
    ingested = await _proposal(page, "Unknown-filter ingested")
    await _set_status(db_session_factory, ingested.id, "ingested")

    html = page.client.get("/admin/knowledge?show=bogus").text

    assert f'data-source-id="{ingested.id}"' in html


async def test_a_decision_keeps_the_filter_it_was_made_under(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-keep-filter")
    approved = await _proposal(page, "Filtered approved")
    waiting = await _proposal(page, "Filtered waiting")

    response = _decide(page, f"{APPROVE}?show=proposed", [approved.id])

    assert f'data-source-id="{approved.id}"' not in response.text
    assert f'data-source-id="{waiting.id}"' in response.text


async def test_a_proposal_shows_its_identifiers_and_detected_line(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-ids")
    pmc = await _proposal(page, "Identified paper", doi=f"10.1186/{uuid4().hex[:8]}")
    detected = await _proposal(
        page,
        "Hypertrophy handbook, ch. 2",
        license_id=LicenseId.CC_BY_NC_ND,
        license_source=LicenseSource.FILE,
        with_file=True,
        license_matched_line="This chapter is licensed under CC BY-NC-ND 4.0.",
    )

    html = unescape(page.client.get("/admin/knowledge").text)

    pmc_html = _source_html(html, pmc.id)
    assert f"{pmc.pmcid} · doi:{pmc.doi}" in pmc_html
    assert pmc_html.count('aria-label="Select this source"') == 2
    detected_html = _source_html(html, detected.id)
    assert detected_html.count("“This chapter is licensed under CC BY-NC-ND 4.0.”") == 2
    assert detected_html.count("detected in the file") == 2
    # A PMC proposal has no stored file: Reject covers it, so no Delete (the mock).
    assert "data-knowledge-delete" not in pmc_html
    assert "data-knowledge-delete" in detected_html


async def test_a_proposal_without_a_licence_offers_set_licence_and_cannot_be_selected(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-no-licence")
    source = await _proposal(
        page, "Coaches' roundtable", license_id=None, license_source=None, with_file=True
    )

    html = unescape(_source_html(page.client.get("/admin/knowledge").text, source.id))

    assert html.count("No licence found in the file. Set one before approving.") == 2
    assert html.count("Set licence") == 2
    assert len(re.findall(r'aria-label="Select this source"[^>]*disabled', html)) == 2


async def test_a_non_proposed_row_has_no_selectable_checkbox(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "proposals-no-pick")
    source = await _proposal(page, "Ingested paper", with_file=True)
    await _set_status(db_session_factory, source.id, "ingested")

    html = _source_html(page.client.get("/admin/knowledge").text, source.id)

    assert 'name="source_id"' not in html
