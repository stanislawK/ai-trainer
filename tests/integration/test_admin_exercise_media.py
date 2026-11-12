"""`/admin/exercises` through the real middleware stack and PostgreSQL (F24, ADR-0031, #554):
approve one suggestion, approve all of an exercise's wger pictures, admin only and CSRF-checked,
no broken image without a public base URL, and a link no longer suggested left as it is."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.media_repository import SqlAlchemyExerciseMediaRepository
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_media_review import ExerciseMediaReview
from ai_trainer.application.media_quarantine import MediaQuarantine
from ai_trainer.domain.media import MediaAsset, MediaLinkStatus
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.templating import build_templates
from tests.admin_tokens_support import memory_admin_tokens
from tests.exercise_media_support import embedded_asset, generated_asset, wger_asset
from tests.knowledge_support import MemoryStorage, knowledge_wiring

SECRET = b"integration-test-secret"
BASE = "https://media.example.org"

_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


@dataclass
class Page:
    client: TestClient
    media: SqlAlchemyExerciseMediaRepository
    factory: Callable[[], AsyncSession]
    csrf: str
    public: MemoryStorage
    private: MemoryStorage

    def store(self, asset: MediaAsset) -> None:
        """Put the stored picture's file in the public media bucket."""
        assert asset.storage_key is not None
        self.public.objects[asset.storage_key] = b"png-bytes"
        self.public.content_types[asset.storage_key] = "image/png"

    def headers(self) -> Mapping[str, str]:
        return {CSRF_HEADER_NAME: self.csrf, "HX-Request": "true"}

    async def exercise(self, name: str) -> UUID:
        exercise_id = uuid4()
        async with self.factory() as session:
            await session.execute(
                text(
                    "INSERT INTO exercises (id, sport_id, source, source_ref, name, "
                    "default_measure) VALUES (:id, 'gym', 'dataset', :ref, :name, 'load_reps')"
                ),
                {"id": exercise_id, "ref": f"zz-review-{exercise_id}", "name": name},
            )
            await session.commit()
        return exercise_id

    async def put(
        self,
        exercise_id: UUID,
        asset: MediaAsset,
        status: MediaLinkStatus = MediaLinkStatus.SUGGESTED,
    ) -> UUID:
        asset_id = await self.media.add_asset(asset)
        position = len(await self.media.review_links(status, exercise_id=exercise_id))
        await self.media.link(exercise_id, asset_id, position=position, status=status)
        [row] = [
            row
            for row in await self.media.review_links(status, exercise_id=exercise_id)
            if row.asset.id == asset_id
        ]
        return row.link_id

    async def status(self, link_id: UUID) -> MediaLinkStatus:
        row = await self.media.review_link(link_id)
        assert row is not None
        return row.status


async def _page(
    factory: Callable[[], AsyncSession],
    sub: str,
    *,
    admin: bool = True,
    base_url: str | None = BASE,
) -> Page:
    users = SqlAlchemyUsersRepository(factory)
    sessions = SqlAlchemySessionsRepository(factory)
    clock = Clock()
    user = await users.create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=clock.now() + timedelta(days=1))
    )
    media = SqlAlchemyExerciseMediaRepository(factory)
    public, private = MemoryStorage(), MemoryStorage()
    templates = build_templates()
    app = FastAPI()
    app.include_router(
        build_admin_router(
            templates=templates,
            users=users,
            sessions=sessions,
            status_changer=SqlAlchemyUserStatusChanger(factory),
            tokens=memory_admin_tokens(),
            exercise_media=ExerciseMediaReview(
                media=media,
                public_base_url=base_url,
                quarantine=MediaQuarantine(media=public, uploads=private),
                clock=clock,
            ),
            **knowledge_wiring(),
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=clock,
        templates=templates,
        admin_emails=[user.email] if admin else [],
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    # Any signed-in page carries the CSRF token; the forbidden page does too.
    match = _CSRF_TOKEN_IN_HTML.search(client.get("/admin/exercises").text)
    assert match is not None
    return Page(
        client=client,
        media=media,
        factory=factory,
        csrf=match.group(1),
        public=public,
        private=private,
    )


def _group(html: str, exercise_id: UUID) -> str:
    match = re.search(
        rf'<section[^>]*data-exercise="{exercise_id}".*?</section>', html, flags=re.DOTALL
    )
    assert match is not None, html
    return match.group(0)


def _rendered_ids(group: str) -> list[UUID]:
    """The link IDs the bulk form posts back."""
    return [
        UUID(value)
        for value in re.findall(r'<input type="hidden" name="link_id" value="([^"]+)"', group)
    ]


async def test_admin_approves_one_suggestion_and_approved_for_returns_it(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-approve-one")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    picture = wger_asset()
    link_id = await page.put(exercise_id, picture)
    listed = page.client.get("/admin/exercises")
    assert f"/admin/exercises/links/{link_id}/approve" in _group(listed.text, exercise_id)

    response = page.client.post(f"/admin/exercises/links/{link_id}/approve", headers=page.headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "data-media-toast" in response.text
    assert "Approved for" in response.text
    [item] = await page.media.approved_for(exercise_id)
    assert item.asset.sha256 == picture.sha256
    approved_view = page.client.get("/admin/exercises?view=approved")
    assert str(link_id) in _group(approved_view.text, exercise_id)


async def test_approve_all_from_wger_leaves_the_generated_picture_suggested(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-approve-all")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    wger_links = [await page.put(exercise_id, wger_asset()) for _ in range(2)]
    generated = await page.put(exercise_id, generated_asset())
    group = _group(page.client.get("/admin/exercises").text, exercise_id)
    assert "Approve all from wger · 2" in group
    assert "Generated · approve on its own" in group
    assert "Check the printed labels" in group
    assert 'type="checkbox"' not in group
    assert _rendered_ids(group) == wger_links

    response = page.client.post(
        f"/admin/exercises/{exercise_id}/approve-wger",
        headers=page.headers(),
        data={"link_id": [str(link) for link in wger_links]},
    )

    assert response.status_code == 200
    assert "Approved 2 pictures from wger." in response.text
    assert "The generated picture still needs its own Approve." in response.text
    assert [await page.status(link) for link in wger_links] == [MediaLinkStatus.APPROVED] * 2
    assert await page.status(generated) is MediaLinkStatus.SUGGESTED
    left = _group(response.text, exercise_id)
    assert "Approve all from wger" not in left
    assert f"/admin/exercises/links/{generated}/approve" in left


async def test_approve_all_from_wger_approves_only_the_posted_ids(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-approve-posted")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    other_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    seen = await page.put(exercise_id, wger_asset())
    generated = await page.put(exercise_id, generated_asset())
    rendered = _rendered_ids(_group(page.client.get("/admin/exercises").text, exercise_id))
    assert rendered == [seen]
    # Imported after the page was drawn, and a wger link on another exercise.
    late = await page.put(exercise_id, wger_asset())
    elsewhere = await page.put(other_id, wger_asset())

    response = page.client.post(
        f"/admin/exercises/{exercise_id}/approve-wger",
        headers=page.headers(),
        data={"link_id": [str(link) for link in (*rendered, generated, elsewhere)]},
    )

    assert response.status_code == 200
    assert "Approved 1 picture from wger." in response.text
    assert await page.status(seen) is MediaLinkStatus.APPROVED
    assert await page.status(generated) is MediaLinkStatus.SUGGESTED
    assert await page.status(late) is MediaLinkStatus.SUGGESTED
    assert await page.status(elsewhere) is MediaLinkStatus.SUGGESTED


async def test_approve_all_from_wger_with_a_malformed_or_too_many_ids_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-approve-malformed")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    link_id = await page.put(exercise_id, wger_asset())
    url = f"/admin/exercises/{exercise_id}/approve-wger"

    malformed = page.client.post(
        url, headers=page.headers(), data={"link_id": [str(link_id), "nope"]}
    )
    flood = page.client.post(
        url, headers=page.headers(), data={"link_id": [str(uuid4()) for _ in range(1000)]}
    )

    assert (malformed.status_code, flood.status_code) == (404, 400)
    assert await page.status(link_id) is MediaLinkStatus.SUGGESTED


async def test_non_admin_gets_the_forbidden_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-not-admin", admin=False)
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    link_id = await page.put(exercise_id, wger_asset())

    responses = [
        page.client.get("/admin/exercises"),
        page.client.get("/admin/exercises", headers={"HX-Request": "true"}),
        page.client.post(f"/admin/exercises/links/{link_id}/approve", headers=page.headers()),
        page.client.post(f"/admin/exercises/{exercise_id}/approve-wger", headers=page.headers()),
        page.client.post("/admin/exercises/links/not-a-uuid/approve", headers=page.headers()),
    ]

    assert [response.status_code for response in responses] == [403] * 5
    assert all("have access to this page" in response.text for response in responses)
    assert await page.status(link_id) is MediaLinkStatus.SUGGESTED


async def test_post_without_csrf_is_refused(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-no-csrf")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    link_id = await page.put(exercise_id, wger_asset())

    one = page.client.post(
        f"/admin/exercises/links/{link_id}/approve", headers={"HX-Request": "true"}
    )
    every = page.client.post(f"/admin/exercises/{exercise_id}/approve-wger")

    assert (one.status_code, every.status_code) == (403, 403)
    assert await page.status(link_id) is MediaLinkStatus.SUGGESTED


async def test_without_a_public_base_url_rows_render_without_an_img(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-no-base", base_url=None)
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    await page.put(exercise_id, wger_asset(author="K. Brandt"))
    await page.put(exercise_id, generated_asset())

    group = _group(page.client.get("/admin/exercises").text, exercise_id)

    assert "<img" not in group
    assert group.count("No public URL") == 2
    assert "K. Brandt" in group
    assert "https://creativecommons.org/licenses/by-sa/4.0/deed.en" in group


async def test_a_stored_picture_shows_from_its_public_url(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-with-base")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    picture = wger_asset(author="R. Okafor", ai=True)
    await page.put(exercise_id, picture)
    await page.put(exercise_id, embedded_asset())

    group = _group(page.client.get("/admin/exercises").text, exercise_id)

    assert f'src="{BASE}/{picture.storage_key}"' in group
    assert "AI generated" in group
    assert "rights not confirmed" in group
    assert "Embedded" in group


async def test_approving_a_link_that_is_no_longer_suggested_changes_nothing_and_says_so(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-stale")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    approved = await page.put(exercise_id, wger_asset(), MediaLinkStatus.APPROVED)
    removed = await page.put(exercise_id, wger_asset(), MediaLinkStatus.REMOVED)

    again = page.client.post(f"/admin/exercises/links/{approved}/approve", headers=page.headers())
    hidden = page.client.post(f"/admin/exercises/links/{removed}/approve", headers=page.headers())

    assert (again.status_code, hidden.status_code) == (409, 409)
    assert "Already approved — nothing changed." in again.text
    assert "It was removed — nothing changed." in hidden.text
    assert await page.status(approved) is MediaLinkStatus.APPROVED
    assert await page.status(removed) is MediaLinkStatus.REMOVED


async def test_an_unknown_or_malformed_id_is_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-unknown")

    responses = [
        page.client.post(f"/admin/exercises/links/{uuid4()}/approve", headers=page.headers()),
        page.client.post("/admin/exercises/links/nope/approve", headers=page.headers()),
        page.client.post("/admin/exercises/nope/approve-wger", headers=page.headers()),
    ]

    assert [response.status_code for response in responses] == [404] * 3


async def test_bulk_approve_with_nothing_waiting_says_so(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-bulk-none")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")

    response = page.client.post(
        f"/admin/exercises/{exercise_id}/approve-wger", headers=page.headers()
    )

    assert response.status_code == 200
    assert "No wger pictures were waiting — nothing changed." in response.text


async def test_the_full_page_and_the_htmx_region(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-region")

    full = page.client.get("/admin/exercises?view=approved")
    region = page.client.get("/admin/exercises?view=bogus", headers={"HX-Request": "true"})

    assert full.status_code == region.status_code == 200
    assert "<html" in full.text
    assert "<html" not in region.text
    assert 'id="exercise-media"' in region.text
    assert 'aria-current="page">' in full.text
    assert re.search(r'aria-pressed="true"[^>]*>.*?Approved ·', full.text, flags=re.DOTALL)
    assert re.search(r'aria-pressed="true"[^>]*>.*?Suggested ·', region.text, flags=re.DOTALL)
    assert full.headers["cache-control"] == "no-store"


async def test_remove_renders_removed_toast_with_undo(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-remove")
    name = f"Lateral Raise {uuid4().hex[:6]}"
    exercise_id = await page.exercise(name)
    picture = wger_asset()
    page.store(picture)
    link_id = await page.put(exercise_id, picture, MediaLinkStatus.APPROVED)
    approved_view = _group(page.client.get("/admin/exercises?view=approved").text, exercise_id)
    remove_url = f"/admin/exercises/links/{link_id}/remove?view=approved"
    assert f'hx-post="{remove_url}"' in approved_view

    response = page.client.post(remove_url, headers=page.headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert f"Removed from {name}." in response.text
    assert "Hidden from the exercise card now." in response.text
    assert f'hx-post="/admin/exercises/links/{link_id}/restore?view=approved"' in response.text
    assert ">Undo<" in response.text
    assert re.search(r'aria-pressed="true"[^>]*>.*?Approved ·', response.text, flags=re.DOTALL)
    assert await page.status(link_id) is MediaLinkStatus.REMOVED
    assert await page.media.approved_for(exercise_id) == []
    assert picture.storage_key not in page.public.objects
    assert page.private.objects == {f"quarantine/{picture.storage_key}": b"png-bytes"}


async def test_removed_view_lists_when_with_restore_and_no_picture(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-removed-view")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    picture = wger_asset(author="T. Weber")
    page.store(picture)
    link_id = await page.put(exercise_id, picture, MediaLinkStatus.APPROVED)
    page.client.post(f"/admin/exercises/links/{link_id}/remove", headers=page.headers())

    response = page.client.get("/admin/exercises?view=removed")

    group = _group(response.text, exercise_id)
    assert "Removed 11 Nov · was approved" in group
    assert "1 picture removed" in group
    assert f'hx-post="/admin/exercises/links/{link_id}/restore?view=removed"' in group
    assert "/remove" not in group
    assert "/approve" not in group
    assert "<img" not in group
    assert "No public URL" not in group
    assert "T. Weber" in group
    assert re.search(r'aria-pressed="true"[^>]*>.*?Removed ·', response.text, flags=re.DOTALL)


async def test_restore_from_the_removed_view_puts_the_picture_back(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-restore")
    name = f"Lateral Raise {uuid4().hex[:6]}"
    exercise_id = await page.exercise(name)
    picture = wger_asset()
    page.store(picture)
    link_id = await page.put(exercise_id, picture)
    page.client.post(f"/admin/exercises/links/{link_id}/remove", headers=page.headers())

    response = page.client.post(
        f"/admin/exercises/links/{link_id}/restore?view=removed", headers=page.headers()
    )

    assert response.status_code == 200
    assert f"Restored to {name}." in response.text
    assert "Back under Suggested." in response.text
    assert await page.status(link_id) is MediaLinkStatus.SUGGESTED
    assert page.public.objects == {str(picture.storage_key): b"png-bytes"}
    assert page.private.objects == {}
    assert str(exercise_id) not in response.text


async def test_remove_says_still_public_when_move_fails(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-still-public")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    picture = wger_asset()
    page.store(picture)
    link_id = await page.put(exercise_id, picture, MediaLinkStatus.APPROVED)
    page.public.failing.add("delete")

    response = page.client.post(
        f"/admin/exercises/links/{link_id}/remove?view=approved", headers=page.headers()
    )

    assert response.status_code == 200
    assert "Hidden from the exercise card, but the file is still public." in response.text
    assert f'hx-post="/admin/exercises/links/{link_id}/remove?view=approved"' in response.text
    assert "Try again" in response.text
    assert await page.status(link_id) is MediaLinkStatus.REMOVED
    assert await page.media.approved_for(exercise_id) == []
    assert picture.storage_key in page.public.objects


async def test_remove_of_a_shared_picture_says_it_stays_public(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-shared")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    other_id = await page.exercise(f"Cable Raise {uuid4().hex[:6]}")
    picture = wger_asset()
    page.store(picture)
    link_id = await page.put(exercise_id, picture, MediaLinkStatus.APPROVED)
    [row] = await page.media.review_links(MediaLinkStatus.APPROVED, exercise_id=exercise_id)
    assert row.asset.id is not None
    await page.media.link(other_id, row.asset.id, position=0, status=MediaLinkStatus.APPROVED)

    response = page.client.post(f"/admin/exercises/links/{link_id}/remove", headers=page.headers())

    assert "another exercise still shows it" in response.text
    assert picture.storage_key in page.public.objects
    assert len(await page.media.approved_for(other_id)) == 1


async def test_restore_that_cannot_move_the_file_back_says_nothing_changed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-restore-failed")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    picture = wger_asset()
    page.store(picture)
    link_id = await page.put(exercise_id, picture, MediaLinkStatus.APPROVED)
    page.client.post(f"/admin/exercises/links/{link_id}/remove", headers=page.headers())
    page.public.failing.add("put")

    response = page.client.post(
        f"/admin/exercises/links/{link_id}/restore?view=removed", headers=page.headers()
    )

    assert response.status_code == 200
    assert "Couldn't restore — nothing changed." in response.text
    assert f'hx-post="/admin/exercises/links/{link_id}/restore?view=removed"' in response.text
    assert await page.status(link_id) is MediaLinkStatus.REMOVED


async def test_remove_and_restore_refuse_non_admin(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-remove-not-admin", admin=False)
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    approved = await page.put(exercise_id, wger_asset(), MediaLinkStatus.APPROVED)
    removed = await page.put(exercise_id, wger_asset(), MediaLinkStatus.REMOVED)

    responses = [
        page.client.post(f"/admin/exercises/links/{approved}/remove", headers=page.headers()),
        page.client.post(f"/admin/exercises/links/{removed}/restore", headers=page.headers()),
        page.client.post("/admin/exercises/links/nope/remove", headers=page.headers()),
        page.client.get("/admin/exercises?view=removed"),
    ]

    assert [response.status_code for response in responses] == [403] * 4
    assert await page.status(approved) is MediaLinkStatus.APPROVED
    assert await page.status(removed) is MediaLinkStatus.REMOVED


async def test_remove_and_restore_refuse_without_csrf(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-remove-no-csrf")
    exercise_id = await page.exercise(f"Lateral Raise {uuid4().hex[:6]}")
    approved = await page.put(exercise_id, wger_asset(), MediaLinkStatus.APPROVED)
    removed = await page.put(exercise_id, wger_asset(), MediaLinkStatus.REMOVED)

    remove = page.client.post(
        f"/admin/exercises/links/{approved}/remove", headers={"HX-Request": "true"}
    )
    restore = page.client.post(f"/admin/exercises/links/{removed}/restore")

    assert (remove.status_code, restore.status_code) == (403, 403)
    assert await page.status(approved) is MediaLinkStatus.APPROVED
    assert await page.status(removed) is MediaLinkStatus.REMOVED


async def test_remove_or_restore_of_an_unknown_link_is_not_found(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-remove-unknown")

    responses = [
        page.client.post(f"/admin/exercises/links/{uuid4()}/remove", headers=page.headers()),
        page.client.post("/admin/exercises/links/nope/restore", headers=page.headers()),
    ]

    assert [response.status_code for response in responses] == [404] * 2


async def test_removed_view_with_nothing_removed_shows_its_empty_card(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    page = await _page(db_session_factory, "media-removed-empty")
    async with page.factory() as session:
        removed = (
            await session.execute(
                text("SELECT count(*) FROM exercise_media WHERE status = 'removed'")
            )
        ).scalar_one()

    response = page.client.get("/admin/exercises?view=removed", headers={"HX-Request": "true"})

    # The shared test database may hold another test's removed rows.
    if removed == 0:
        assert "Nothing removed" in response.text
    assert "Removed ·" in response.text
