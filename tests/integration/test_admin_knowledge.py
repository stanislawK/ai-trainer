"""The admin's knowledge page through the real middleware stack and PostgreSQL (ADR-0033, #422):
upload, refusals, re-upload, delete and re-ingest, admin only and CSRF-checked."""

import re
from html import unescape
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.domain.knowledge import (
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    SourceStatus,
    content_hash,
)
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.templating import build_templates
from tests.admin_tokens_support import memory_admin_tokens
from tests.knowledge_support import MemoryStorage, answers, pdf_bytes

FROZEN_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SECRET = b"integration-test-secret"
MAX_MB = 1

_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


@dataclass
class Page:
    client: TestClient
    sources: SqlAlchemyKnowledgeSourcesRepository
    storage: MemoryStorage
    csrf: str
    admin: User

    def upload(
        self, data: bytes, *, name: str = "deload-timing.pdf", **overrides: str | list[str]
    ) -> httpx2.Response:
        return self.client.post(
            "/admin/knowledge/sources",
            data=answers(**overrides),
            files={"file": (name, data, "application/octet-stream")},
            headers={CSRF_HEADER_NAME: self.csrf, "HX-Request": "true"},
        )

    def headers(self) -> Mapping[str, str]:
        return {CSRF_HEADER_NAME: self.csrf, "HX-Request": "true"}


async def _user(users: SqlAlchemyUsersRepository, sub: str) -> User:
    return await users.create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )


async def _page(factory: Callable[[], AsyncSession], sub: str, *, admin: bool = True) -> Page:
    users = SqlAlchemyUsersRepository(factory)
    sessions = SqlAlchemySessionsRepository(factory)
    sources = SqlAlchemyKnowledgeSourcesRepository(factory)
    storage = MemoryStorage()
    user = await _user(users, sub)
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    registry = default_sport_registry()
    app = FastAPI()
    templates = build_templates()
    app.include_router(
        build_admin_router(
            templates=templates,
            users=users,
            sessions=sessions,
            status_changer=SqlAlchemyUserStatusChanger(factory),
            knowledge=KnowledgeSources(
                sources=sources,
                storage=storage,
                clock=FakeClock(),
                max_upload_bytes=MAX_MB * 1024 * 1024,
                sports=[sport.id for sport in registry.all()],
            ),
            tokens=memory_admin_tokens(),
            sports=registry,
            max_upload_mb=MAX_MB,
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=[user.email] if admin else [],
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    return Page(client=client, sources=sources, storage=storage, csrf=_csrf(client), admin=user)


def _csrf(client: TestClient) -> str:
    # A non-admin's 403 page is a full page on the same layout, so it carries the token too.
    page = client.get("/admin/knowledge")
    match = _CSRF_TOKEN_IN_HTML.search(page.text)
    assert match is not None, page.text
    return match.group(1)


async def _row_for(page: Page, data: bytes) -> KnowledgeSource | None:
    return await page.sources.get_by_hash(content_hash(data))


async def _set_status(
    factory: Callable[[], AsyncSession], source_id: UUID, status: str, error: str | None = None
) -> None:
    async with factory() as session:
        await session.execute(
            text("UPDATE knowledge_sources SET status = :s, error = :e WHERE id = :id"),
            {"s": status, "e": error, "id": source_id},
        )
        await session.commit()


def _source_html(html: str, source_id: UUID) -> str:
    """Every element that renders this source, joined, so a test reads its status and actions."""
    found = re.findall(rf'<[^>]+data-source-id="{source_id}".*?</(?:li|tr)>', html, re.S)
    assert found, html
    return "\n".join(found)


# --- AC 1: an admin uploads a PDF, sees it listed as uploaded, and the file is in the bucket --


async def test_an_admin_uploads_a_pdf_and_sees_it_listed_as_uploaded(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-ac1")
    data = pdf_bytes("ac1")

    response = page.upload(data)

    assert response.status_code == 200
    source = await _row_for(page, data)
    assert source is not None
    assert source.status is SourceStatus.UPLOADED
    assert source.uploaded_by_user_id == page.admin.id
    assert source.storage_key is not None
    assert page.storage.objects[source.storage_key] == data
    listed = _source_html(response.text, source.id)
    assert "Deload timing in resistance training" in listed
    assert 'data-status="uploaded"' in listed
    assert "Uploaded" in listed
    # And a fresh page load lists it too.
    assert 'data-status="uploaded"' in _source_html(
        page.client.get("/admin/knowledge").text, source.id
    )


async def _licence_row(factory: Callable[[], AsyncSession], data: bytes) -> tuple[object, ...]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, license_id, license_source, license_url,"
                    " copyright_statement, attribution, license_note"
                    " FROM knowledge_sources WHERE content_hash = :hash"
                ),
                {"hash": content_hash(data)},
            )
        ).one()
    return tuple(row)


# --- #456 AC 1: a CC BY upload stores `license_id = cc-by` with its attribution --------------


async def test_a_cc_by_upload_stores_its_licence_and_attribution(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-ac1")
    data = pdf_bytes("456-ac1")

    # A note left in the hidden textarea is not part of a CC licence.
    response = page.upload(data, license_note="leftover")

    assert response.status_code == 200
    assert await _licence_row(db_session_factory, data) == (
        "uploaded",
        "cc-by",
        "admin",
        "https://creativecommons.org/licenses/by/4.0/",
        "© 2024 The Authors",
        "M. Kowalska et al., Deload timing in resistance training, CC BY 4.0",
        None,
    )
    source = await _row_for(page, data)
    assert source is not None
    assert (source.license_id, source.license_source) == (LicenseId.CC_BY, LicenseSource.ADMIN)
    assert '<span class="text-[13px]">CC BY</span>' in _source_html(response.text, source.id)


async def test_an_other_upload_stores_the_permission_held(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-other")
    data = pdf_bytes("456-other")

    response = page.upload(
        data, license_id="other", license_note="The author's written permission, 12 Oct 2026."
    )

    assert response.status_code == 200
    row = await _licence_row(db_session_factory, data)
    assert (row[1], row[6]) == ("other", "The author's written permission, 12 Oct 2026.")


# --- The #454 amendment: Detect from the file is the default ---------------------------------


async def test_a_detect_upload_is_stored_detecting_with_no_licence(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-detect")
    data = pdf_bytes("456-detect")

    response = page.upload(data, license_id="detect", attribution="")

    assert response.status_code == 200
    assert await _licence_row(db_session_factory, data) == (
        "detecting",
        None,
        None,
        None,
        None,
        None,
        None,
    )
    source = await _row_for(page, data)
    assert source is not None
    listed = _source_html(response.text, source.id)
    assert 'data-status="detecting"' in listed
    assert "Detecting" in listed
    assert "loading-spinner" in listed
    # Nothing to ingest or re-ingest until a licence is confirmed.
    assert "/reingest" not in listed


async def test_the_sheet_opens_on_detect_from_the_file(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-default")

    html = page.client.get("/admin/knowledge").text

    select = re.search(r'<select[^>]*name="license_id".*?</select>', html, re.S)
    assert select is not None
    options = re.findall(r'<option value="([^"]*)"( selected)?', select.group(0))
    assert options[0] == ("detect", " selected")
    assert [value for value, _ in options] == [
        "detect",
        "cc0",
        "cc-by",
        "cc-by-sa",
        "cc-by-nc",
        "cc-by-nc-sa",
        "cc-by-nd",
        "cc-by-nc-nd",
        "other",
    ]
    assert "Upload and detect licence" in html


async def test_the_page_lists_the_upload_form_with_its_choices(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-form")

    html = page.client.get("/admin/knowledge").text

    assert 'hx-encoding="multipart/form-data"' in html
    assert 'accept=".pdf,.epub,.md,.markdown,.txt,.vtt,.srt"' in html
    assert f'data-max-bytes="{MAX_MB * 1024 * 1024}"' in html
    for value in ("peer_reviewed", "coaching_book", "article_or_transcript"):
        assert f'value="{value}"' in html
    for value in ("research_paper", "book", "talk_or_podcast", "article"):
        assert f'value="{value}"' in html
    assert 'value="catalogue"' not in html
    for sport in ("gym", "climbing", "cycling"):
        assert f'name="sports" value="{sport}"' in html
    assert 'aria-current="page"' in html
    assert "Knowledge" in html


# --- AC 2: a non-admin gets 403 on every route ----------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/knowledge"),
        ("POST", "/admin/knowledge/sources"),
        ("DELETE", "/admin/knowledge/sources/{id}"),
        ("POST", "/admin/knowledge/sources/{id}/reingest"),
        ("DELETE", "/admin/knowledge/sources/not-a-uuid"),
        ("POST", "/admin/knowledge/sources/not-a-uuid/reingest"),
    ],
    ids=["page", "upload", "delete", "reingest", "delete-bad-id", "reingest-bad-id"],
)
async def test_a_non_admin_gets_403_on_every_knowledge_route(
    db_session_factory: Callable[[], AsyncSession], method: str, path: str
) -> None:
    owner = await _page(db_session_factory, f"knowledge-ac2-admin-{method}-{len(path)}")
    kept_data = pdf_bytes(f"ac2-{method}-{path}")
    owner.upload(kept_data)
    kept = await _row_for(owner, kept_data)
    assert kept is not None
    assert kept.storage_key is not None
    await _set_status(db_session_factory, kept.id, "failed", "no_text")
    page = await _page(db_session_factory, f"knowledge-ac2-user-{method}-{len(path)}", admin=False)
    sneaked = pdf_bytes(f"ac2-sneak-{method}-{path}")

    response = page.client.request(
        method,
        path.format(id=kept.id),
        data=answers() if path.endswith("sources") else None,
        files={"file": ("x.pdf", sneaked, "application/pdf")} if path.endswith("sources") else None,
        headers=page.headers(),
    )

    assert response.status_code == 403
    assert await _row_for(page, sneaked) is None
    still = await owner.sources.get(kept.id)
    assert still is not None
    assert still.status is SourceStatus.FAILED
    assert kept.storage_key in owner.storage.objects


async def test_a_malformed_upload_body_from_a_non_admin_is_403_not_422(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-ac2-malformed", admin=False)

    response = page.client.post(
        "/admin/knowledge/sources",
        content=b"--not-multipart",
        headers={**page.headers(), "Content-Type": "multipart/form-data; boundary=x"},
    )

    assert response.status_code == 403


async def test_an_upload_without_a_csrf_token_is_refused(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-csrf")
    data = pdf_bytes("csrf")

    response = page.client.post(
        "/admin/knowledge/sources",
        data=answers(),
        files={"file": ("a.pdf", data, "application/pdf")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403
    assert await _row_for(page, data) is None


# --- AC 3: no evidence tier or license note is refused inline, with nothing stored -----------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"evidence_tier": ""}, "Pick an evidence tier."),
        ({"evidence_tier": "influencer"}, "Pick an evidence tier."),
        ({"attribution": "   "}, "Add an attribution."),
        ({"license_url": "creativecommons.org"}, "Use a full link that starts with http://"),
    ],
    ids=["no-tier", "unknown-tier", "blank-attribution", "licence-url"],
)
async def test_an_upload_without_a_tier_or_attribution_is_refused_inline_and_stores_nothing(
    db_session_factory: Callable[[], AsyncSession],
    overrides: dict[str, str],
    message: str,
) -> None:
    page = await _page(db_session_factory, f"knowledge-ac3-{next(iter(overrides))}-{len(message)}")
    data = pdf_bytes(f"ac3-{overrides}")

    response = page.upload(data, **overrides, title="Kept title")

    assert response.status_code == 422
    html = response.text
    assert message in html
    assert 'aria-invalid="true"' in html
    assert 'data-open="true"' in html
    assert 'value="Kept title"' in html
    assert await _row_for(page, data) is None
    assert page.storage.objects == {}


# --- AC 4: a file too large or of an unlisted type is refused inline -------------------------


async def test_a_file_over_the_upload_limit_is_refused_inline(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-ac4-large")
    data = pdf_bytes("ac4-large") + b"x" * (MAX_MB * 1024 * 1024)

    response = page.upload(data, name="handbook-full.pdf")

    assert response.status_code == 422
    html = response.text
    assert f"The limit is {MAX_MB} MB." in html
    assert "border-error" in html
    assert 'data-open="true"' in html
    assert await _row_for(page, data) is None
    assert page.storage.objects == {}


@pytest.mark.parametrize(
    ("name", "data"),
    [("notes.docx", b"PK\x03\x04 a word file"), ("scan.pdf", b"<html>not a pdf</html>")],
    ids=["docx", "pdf-that-is-not"],
)
async def test_a_file_of_an_unlisted_type_is_refused_inline(
    db_session_factory: Callable[[], AsyncSession], name: str, data: bytes
) -> None:
    page = await _page(db_session_factory, f"knowledge-ac4-type-{name}")
    data += uuid4().bytes

    response = page.upload(data, name=name)

    assert response.status_code == 422
    assert "That file type isn't supported." in response.text
    assert await _row_for(page, data) is None
    assert page.storage.objects == {}


# --- AC 5: re-uploading an ingested file changes nothing; a failed one says so ---------------


async def _stored_with_status(
    page: Page, factory: Callable[[], AsyncSession], marker: str, status: str, error: str | None
) -> tuple[bytes, KnowledgeSource]:
    data = pdf_bytes(marker)
    page.upload(data)
    source = await _row_for(page, data)
    assert source is not None
    await _set_status(factory, source.id, status, error)
    refreshed = await page.sources.get(source.id)
    assert refreshed is not None
    return data, refreshed


async def test_re_uploading_an_ingested_file_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-ac5-ingested")
    data, before = await _stored_with_status(page, db_session_factory, "ac5-ing", "ingested", None)
    objects = dict(page.storage.objects)

    response = page.upload(data, name="renamed.pdf", title="Another title")

    assert response.status_code == 409
    html = response.text
    assert "This file is already ingested as" in html
    assert "Nothing changed." in html
    assert await page.sources.get(before.id) == before
    assert page.storage.objects == objects


async def test_re_uploading_a_failed_file_says_its_last_ingest_failed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-ac5-failed")
    data, before = await _stored_with_status(
        page, db_session_factory, "ac5-fail", "failed", "No text could be read."
    )

    response = page.upload(data)

    assert response.status_code == 409
    html = response.text
    assert "and its last ingest failed" in html
    assert "No text could be read." in html
    assert f'hx-post="/admin/knowledge/sources/{before.id}/reingest"' in html
    assert await page.sources.get(before.id) == before


@pytest.mark.parametrize("status", ["uploaded", "ingesting", "detecting", "proposed"])
async def test_re_uploading_a_file_still_waiting_names_its_status(
    db_session_factory: Callable[[], AsyncSession], status: str
) -> None:
    page = await _page(db_session_factory, f"knowledge-ac5-{status}")
    data, before = await _stored_with_status(
        page, db_session_factory, f"ac5-{status}", status, None
    )

    response = page.upload(data)

    assert response.status_code == 409
    html = response.text
    assert f"still {status}" in html
    assert "Nothing changed." in html
    assert await page.sources.get(before.id) == before


# --- AC 6: delete removes the file and the source --------------------------------------------


async def test_delete_removes_the_file_and_the_source(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-ac6")
    data = pdf_bytes("ac6")
    page.upload(data)
    source = await _row_for(page, data)
    assert source is not None
    assert source.storage_key is not None

    response = page.client.delete(f"/admin/knowledge/sources/{source.id}", headers=page.headers())

    assert response.status_code == 200
    assert await page.sources.get(source.id) is None
    assert source.storage_key not in page.storage.objects
    assert f'data-source-id="{source.id}"' not in response.text


async def test_a_failed_storage_delete_keeps_the_source(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-ac6-fail")
    data = pdf_bytes("ac6-fail")
    page.upload(data)
    source = await _row_for(page, data)
    assert source is not None
    page.storage.failing.add("delete")

    response = page.client.delete(f"/admin/knowledge/sources/{source.id}", headers=page.headers())

    assert response.status_code == 503
    assert "Nothing was deleted." in response.text
    assert await page.sources.get(source.id) == source


@pytest.mark.parametrize(
    "path",
    [f"/admin/knowledge/sources/{uuid4()}", "/admin/knowledge/sources/x"],
    ids=["unknown", "malformed"],
)
async def test_deleting_an_unknown_source_is_404(
    db_session_factory: Callable[[], AsyncSession], path: str
) -> None:
    page = await _page(db_session_factory, f"knowledge-ac6-404-{len(path)}")

    assert page.client.delete(path, headers=page.headers()).status_code == 404


# --- Re-ingest ------------------------------------------------------------------------------


async def test_re_ingest_puts_a_failed_source_back_as_uploaded(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-reingest")
    _, source = await _stored_with_status(page, db_session_factory, "reingest", "failed", "boom")

    response = page.client.post(
        f"/admin/knowledge/sources/{source.id}/reingest", headers=page.headers()
    )

    assert response.status_code == 200
    after = await page.sources.get(source.id)
    assert after is not None
    assert (after.status, after.error) == (SourceStatus.UPLOADED, None)
    assert 'data-status="uploaded"' in _source_html(response.text, source.id)


async def test_re_ingest_of_a_waiting_source_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-reingest-wait")
    data = pdf_bytes("reingest-wait")
    page.upload(data)
    source = await _row_for(page, data)
    assert source is not None

    response = page.client.post(
        f"/admin/knowledge/sources/{source.id}/reingest", headers=page.headers()
    )

    assert response.status_code == 409
    assert await page.sources.get(source.id) == source


@pytest.mark.parametrize(
    "path",
    [f"/admin/knowledge/sources/{uuid4()}/reingest", "/admin/knowledge/sources/x/reingest"],
    ids=["unknown", "malformed"],
)
async def test_re_ingest_of_an_unknown_source_is_404(
    db_session_factory: Callable[[], AsyncSession], path: str
) -> None:
    page = await _page(db_session_factory, f"knowledge-reingest-404-{len(path)}")

    assert page.client.post(path, headers=page.headers()).status_code == 404


# --- Storage ---------------------------------------------------------------------------------


async def test_a_failed_put_is_refused_inline_and_stores_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-put-fails")
    page.storage.failing.add("put")
    data = pdf_bytes("put-fails")

    response = page.upload(data)

    assert response.status_code == 503
    assert "The file couldn't be stored." in response.text
    assert await _row_for(page, data) is None


async def test_re_uploading_a_rejected_file_says_delete_it_to_upload_again(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-rejected")
    data, before = await _stored_with_status(
        page, db_session_factory, "456-rejected", "rejected", None
    )

    response = page.upload(data)

    assert response.status_code == 409
    html = response.text
    assert "This file was rejected as “Deload timing in resistance training”." in html
    assert "Delete it from the list to upload it again." in html
    assert await page.sources.get(before.id) == before


# --- #456 AC 2: `other` without a note keeps the sheet open with the error -------------------


async def test_other_without_a_note_reopens_the_sheet_with_its_error(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-ac2")
    data = pdf_bytes("456-ac2")

    response = page.upload(data, license_id="other", license_note="  ", title="Kept title")

    assert response.status_code == 422
    html = response.text
    assert 'data-open="true"' in html
    assert re.search(r'<textarea[^>]*name="license_note"[^>]*aria-invalid="true"', html)
    note_error = re.search(r'<p[^>]*data-error-for="license_note"[^>]*>([^<]*)</p>', html)
    assert note_error is not None
    assert "hidden" not in note_error.group(0)
    assert note_error.group(1) == "Say what permission you hold. “Other” needs it."
    # The sheet comes back as it was posted: Other picked, its fields shown, answers kept.
    assert '<option value="other" selected' in html
    assert 'value="Kept title"' in html
    assert "Upload and ingest" in html
    assert await _row_for(page, data) is None
    assert page.storage.objects == {}


async def test_a_reopened_sheet_shows_the_licence_the_server_checked(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    # The server strips the pick, so the sheet must too, or the marked field would be hidden.
    page = await _page(db_session_factory, "knowledge-456-padded")
    data = pdf_bytes("456-padded")

    response = page.upload(data, license_id=" cc-by ", attribution="")

    assert response.status_code == 422
    html = response.text
    assert '<option value="cc-by" selected' in html
    assert re.search(
        r'<fieldset class="fieldset" data-licence-field>\s*<legend[^>]*>Attribution', html
    )
    assert re.search(r'<input[^>]*name="attribution"[^>]*aria-invalid="true"', html)
    assert "Upload and ingest" in html


# --- #456 AC 3: the server, not only the browser, refuses an unknown licence -----------------


@pytest.mark.parametrize("license_id", ["cc-by-4.0", "tdm", "implied-oa", ""])
async def test_an_unknown_licence_is_refused_by_the_server(
    db_session_factory: Callable[[], AsyncSession], license_id: str
) -> None:
    page = await _page(db_session_factory, f"knowledge-456-ac3-{license_id or 'blank'}")
    data = pdf_bytes(f"456-ac3-{license_id}")

    # Posted straight to the route, past every check the browser makes.
    response = page.upload(data, license_id=license_id)

    assert response.status_code == 422
    assert re.search(r'<select[^>]*name="license_id"[^>]*aria-invalid="true"', response.text)
    assert "Pick a licence." in response.text
    assert await _row_for(page, data) is None
    assert page.storage.objects == {}


# --- #456 AC 4: a `cc-by-sa` or `cc-by-nd` source shows its flag in the list ----------------


async def _seeded(page: Page, marker: str, license_id: str) -> UUID:
    data = pdf_bytes(marker)
    response = page.upload(data, license_id=license_id, title=f"Licensed {marker}")
    assert response.status_code == 200, response.text
    source = await _row_for(page, data)
    assert source is not None
    return source.id


async def test_share_alike_and_no_derivatives_sources_show_their_flag(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-ac4")
    flagged = {
        await _seeded(page, "456-sa", "cc-by-sa"): ("CC BY-SA", "SA · review", "Share-alike"),
        await _seeded(page, "456-nd", "cc-by-nd"): ("CC BY-ND", "ND · review", "No deriv"),
        await _seeded(page, "456-ncsa", "cc-by-nc-sa"): ("CC BY-NC-SA", "SA · review", "Sh"),
        await _seeded(page, "456-ncnd", "cc-by-nc-nd"): ("CC BY-NC-ND", "ND · review", "No"),
    }
    plain = await _seeded(page, "456-by", "cc-by")

    html = page.client.get("/admin/knowledge").text

    for source_id, (label, flag, reason) in flagged.items():
        listed = _source_html(html, source_id)
        assert label in listed
        # Once in the phone's card and once in the desktop row, each titled with its reason.
        titles = re.findall(
            r'<span class="badge badge-soft badge-warning[^"]*" title="([^"]+)">' + re.escape(flag),
            listed,
        )
        assert len(titles) == 2, listed
        assert all(t.startswith(reason) and "a lawyer has read its terms" in t for t in titles)
    assert "badge-warning" not in _source_html(html, plain)


# --- #456 AC 5: #422's sources still list and re-ingest -------------------------------------


async def test_a_422_source_still_lists_and_re_ingests(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-456-ac5")
    source_id = uuid4()
    # #422's insert named no licence column: #453's defaults made it `other`, set by an admin.
    async with db_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO knowledge_sources (id, title, source_type, evidence_tier, language,"
                " sports, license_note, content_hash, file_type, file_name, size_bytes,"
                " storage_key, status, error, uploaded_at)"
                " VALUES (:id, 'Hangboard protocols', 'book', 'coaching_book', 'en',"
                " ARRAY['climbing'], 'Owner''s copy', :hash, 'pdf', 'h.pdf', 10, :key,"
                " 'failed', 'no_text', :at)"
            ),
            {
                "id": source_id,
                "hash": uuid4().hex * 2,
                "key": f"sources/{source_id}",
                "at": FROZEN_NOW,
            },
        )
        await session.commit()

    listed = _source_html(page.client.get("/admin/knowledge").text, source_id)
    assert "Hangboard protocols" in listed
    assert "Other, with permission" in listed
    assert 'data-status="failed"' in listed

    response = page.client.post(
        f"/admin/knowledge/sources/{source_id}/reingest", headers=page.headers()
    )

    assert response.status_code == 200
    after = await page.sources.get(source_id)
    assert after is not None
    assert (after.status, after.license_id, after.license_note) == (
        SourceStatus.UPLOADED,
        LicenseId.OTHER,
        "Owner's copy",
    )


# --- #423: the chunks line and the failed source's reason ----------------------------------


async def _add_chunks(
    factory: Callable[[], AsyncSession], source_id: UUID, count: int, model: str
) -> None:
    vector = "[" + ",".join(["0.1"] * 1024) + "]"
    async with factory() as session:
        for ordinal in range(count):
            await session.execute(
                text(
                    "INSERT INTO knowledge_chunks (id, source_id, ordinal, text, embedding,"
                    " embedding_model, sports, language, section_kind, locator, search_vector,"
                    " created_at) VALUES (:id, :source, :ordinal, 'Deload.',"
                    " CAST(:vector AS vector), :model, '{gym}', 'en', 'body',"
                    " CAST(:locator AS jsonb), 'deload', :at)"
                ),
                {
                    "id": uuid4(),
                    "source": source_id,
                    "ordinal": ordinal,
                    "vector": vector,
                    "model": model,
                    "locator": '{"kind": "section"}',
                    "at": FROZEN_NOW,
                },
            )
        await session.commit()


@pytest.mark.parametrize(
    ("count", "line"),
    [(214, "214 chunks · qwen/qwen3-embedding-8b"), (1, "1 chunk · qwen/qwen3-embedding-8b")],
    ids=["many", "one"],
)
async def test_an_ingested_source_shows_its_chunk_count_and_model(
    db_session_factory: Callable[[], AsyncSession], count: int, line: str
) -> None:
    page = await _page(db_session_factory, f"knowledge-chunks-{count}")
    _, source = await _stored_with_status(
        page, db_session_factory, f"chunks-{count}", "ingested", None
    )
    await _add_chunks(db_session_factory, source.id, count, "qwen/qwen3-embedding-8b")

    html = _source_html(page.client.get("/admin/knowledge").text, source.id)

    # Once in the phone card and once in the desktop row's source cell (the mock's meta line).
    assert html.count(line) == 2


async def test_a_source_without_chunks_shows_no_chunks_line(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "knowledge-no-chunks")
    _, source = await _stored_with_status(page, db_session_factory, "no-chunks", "uploaded", None)

    html = _source_html(page.client.get("/admin/knowledge").text, source.id)

    assert "chunk" not in html


@pytest.mark.parametrize(
    ("error", "copy"),
    [
        ("no_reader", "PDF files can't be read yet."),
        ("not_utf8", "The file isn't UTF-8 text, so it couldn't be read."),
        ("no_text", "The file has no text to ingest."),
        ("file_missing", "The file is missing from storage."),
        ("embedding_failed", "The embedding call failed. Try Re-ingest."),
        ("rate_limited", "OpenRouter kept answering “too many requests”. Try Re-ingest later."),
        ("no_acting_admin", "The admin who uploaded it no longer has an account."),
        ("Something only #422 wrote.", "Something only #422 wrote."),
    ],
    ids=["no_reader", "not_utf8", "no_text", "missing", "embedding", "rate", "orphan", "free"],
)
async def test_a_failed_source_words_its_reason(
    db_session_factory: Callable[[], AsyncSession], error: str, copy: str
) -> None:
    page = await _page(db_session_factory, f"knowledge-reason-{error[:12]}")
    _, source = await _stored_with_status(
        page, db_session_factory, f"reason-{error}", "failed", error
    )

    html = unescape(_source_html(page.client.get("/admin/knowledge").text, source.id))

    assert html.count(copy) == 2
    if error != copy:
        assert error not in html
