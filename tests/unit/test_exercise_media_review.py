"""The admin's F24 review of exercise media (ADR-0031 *Media links*, #554): pictures grouped
per exercise, their credit and public URL, one Approve, and Approve all from wger only."""

from datetime import datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.exercise_media_review import (
    Approved,
    BulkApproved,
    ExerciseMediaReview,
    NotSuggested,
    Removed,
    Restored,
    RestoreFailed,
)
from ai_trainer.application.media_credit import WgerCredit
from ai_trainer.domain.media import MediaAsset, MediaLinkStatus, MediaRepositoryError
from ai_trainer.domain.object_storage import ObjectStorageOperation
from tests.exercise_media_support import (
    REVIEW_NOW,
    FakeCdn,
    MemoryExerciseMedia,
    embedded_asset,
    generated_asset,
    memory_exercise_media_review,
    wger_asset,
)
from tests.knowledge_support import MemoryStorage

BASE = "https://media.example.org/"


async def test_suggested_page_groups_pictures_per_exercise_with_credit_and_url() -> None:
    media = MemoryExerciseMedia()
    squat = media.exercise("Barbell Back Squat")
    raise_ = media.exercise("Dumbbell Lateral Raise")
    plain = wger_asset(author="K. Brandt")
    flagged = wger_asset(author="R. Okafor", ai=True)
    media.put(raise_, plain)
    media.put(raise_, flagged)
    media.put(raise_, generated_asset())
    media.put(squat, embedded_asset())
    media.put(squat, wger_asset(), MediaLinkStatus.APPROVED)
    review = memory_exercise_media_review(media, base_url=BASE)

    page = await review.page(MediaLinkStatus.SUGGESTED)

    assert page.view is MediaLinkStatus.SUGGESTED
    assert page.counts == {
        MediaLinkStatus.SUGGESTED: 4,
        MediaLinkStatus.APPROVED: 1,
        MediaLinkStatus.REMOVED: 0,
    }
    assert [(group.name, group.wger_suggested) for group in page.groups] == [
        ("Barbell Back Squat", 0),
        ("Dumbbell Lateral Raise", 2),
    ]
    [video] = page.groups[0].pictures
    assert (video.origin, video.public_url) == ("embedded", None)
    assert video.page_url is not None
    assert video.page_url.startswith("https://www.youtube.com/embed/")
    first, second, made = page.groups[1].pictures
    assert first.origin == "wger"
    assert first.public_url == f"https://media.example.org/{plain.storage_key}"
    assert first.credit == WgerCredit(
        author="K. Brandt",
        licence="CC-BY-SA 4",
        licence_url="https://creativecommons.org/licenses/by-sa/4.0/deed.en",
    )
    assert first.page_url == plain.source_ref
    assert not first.ai_flagged
    assert second.ai_flagged
    assert second.page_url == flagged.source_ref.removesuffix("#ai-generated")
    assert made.origin == "generated"
    assert made.credit is None
    assert made.attribution is not None
    assert made.attribution.startswith("AI generated with")
    assert not made.ai_flagged


async def test_review_row_has_no_url_without_a_base() -> None:
    media = MemoryExerciseMedia()
    media.put(media.exercise("Row"), wger_asset())
    review = memory_exercise_media_review(media, base_url=None)

    [group] = (await review.page(MediaLinkStatus.SUGGESTED)).groups

    [picture] = group.pictures
    assert picture.public_url is None
    assert picture.credit is not None


async def test_a_wger_attribution_that_does_not_parse_keeps_its_text() -> None:
    media = MemoryExerciseMedia()
    odd = wger_asset().model_copy(update={"attribution": "Someone, somewhere"})
    media.put(media.exercise("Row"), odd)

    [group] = (await memory_exercise_media_review(media).page(MediaLinkStatus.SUGGESTED)).groups

    assert group.pictures[0].credit is None
    assert group.pictures[0].attribution == "Someone, somewhere"


async def test_another_source_is_neither_wger_nor_generated() -> None:
    media = MemoryExerciseMedia()
    media.put(media.exercise("Row"), wger_asset().model_copy(update={"source": "drawn"}))

    [group] = (await memory_exercise_media_review(media).page(MediaLinkStatus.SUGGESTED)).groups

    assert group.pictures[0].origin == "other"
    assert group.wger_suggested == 0


async def test_approved_page_lists_approved_links_without_a_bulk_count() -> None:
    media = MemoryExerciseMedia()
    row = media.exercise("Row")
    media.put(row, wger_asset(), MediaLinkStatus.APPROVED)
    media.put(row, wger_asset())

    [group] = (await memory_exercise_media_review(media).page(MediaLinkStatus.APPROVED)).groups

    assert [picture.status for picture in group.pictures] == [MediaLinkStatus.APPROVED]
    assert group.wger_suggested == 0


async def test_approve_one_suggestion() -> None:
    media = MemoryExerciseMedia()
    row = media.exercise("Row")
    link = media.put(row, generated_asset())
    review = memory_exercise_media_review(media)

    assert await review.approve(link) == Approved(exercise_name="Row")
    assert len(await media.approved_for(row)) == 1


async def test_approving_a_link_no_longer_suggested_changes_nothing_and_says_so() -> None:
    media = MemoryExerciseMedia()
    row = media.exercise("Row")
    approved = media.put(row, wger_asset(), MediaLinkStatus.APPROVED)
    removed = media.put(row, wger_asset(), MediaLinkStatus.REMOVED)
    review = memory_exercise_media_review(media)

    assert await review.approve(approved) == NotSuggested(
        exercise_name="Row", status=MediaLinkStatus.APPROVED
    )
    assert await review.approve(removed) == NotSuggested(
        exercise_name="Row", status=MediaLinkStatus.REMOVED
    )
    assert media.status_of(removed) is MediaLinkStatus.REMOVED
    assert await review.approve(uuid4()) is None


async def test_approve_all_from_wger_leaves_generated_pictures_suggested() -> None:
    media = MemoryExerciseMedia()
    row = media.exercise("Row")
    for asset in (wger_asset(), wger_asset(), generated_asset(), embedded_asset()):
        media.put(row, asset)
    generated = media.links[2].link_id
    posted = [link.link_id for link in media.links]
    review = memory_exercise_media_review(media)

    assert await review.approve_all_from_wger(row, posted) == BulkApproved(
        count=2, generated_left=1
    )
    assert media.status_of(generated) is MediaLinkStatus.SUGGESTED
    assert await review.approve_all_from_wger(row, posted) == BulkApproved(
        count=0, generated_left=1
    )


async def test_approve_all_from_wger_approves_only_the_posted_links() -> None:
    media = MemoryExerciseMedia()
    row = media.exercise("Row")
    seen = media.put(row, wger_asset())
    late = media.put(row, wger_asset())
    review = memory_exercise_media_review(media)

    assert await review.approve_all_from_wger(row, [seen]) == BulkApproved(
        count=1, generated_left=0
    )
    assert media.status_of(late) is MediaLinkStatus.SUGGESTED


class _Takedown:
    """A review over memory links and two memory buckets, with one stored picture in place."""

    def __init__(
        self,
        status: MediaLinkStatus = MediaLinkStatus.APPROVED,
        asset: MediaAsset | None = None,
        *,
        buckets: bool = True,
        media: MemoryExerciseMedia | None = None,
        cdn: FakeCdn | None = None,
        base_url: str | None = BASE,
    ) -> None:
        self.cdn = cdn
        self.media = media or MemoryExerciseMedia()
        self.exercise = self.media.exercise("Dumbbell Lateral Raise")
        self.asset = asset or wger_asset()
        self.link = self.media.put(self.exercise, self.asset, status)
        self.public = MemoryStorage()
        self.private = MemoryStorage()
        if self.asset.storage_key is not None:
            self.public.objects[self.key] = b"png-bytes"
            self.public.content_types[self.key] = "image/png"
        self.review: ExerciseMediaReview = memory_exercise_media_review(
            self.media,
            base_url=base_url,
            cdn=cdn,
            media_bucket=self.public if buckets else None,
            uploads_bucket=self.private if buckets else None,
        )

    @property
    def key(self) -> str:
        assert self.asset.storage_key is not None
        return self.asset.storage_key

    @property
    def quarantined(self) -> str:
        return f"quarantine/{self.key}"

    def is_public(self) -> bool:
        return self.key in self.public.objects


async def test_remove_moves_stored_object_to_quarantine() -> None:
    t = _Takedown()

    outcome = await t.review.remove(t.link)

    # No CDN adapter in this fixture, so the cache was not purged.
    assert outcome == Removed(
        exercise_name="Dumbbell Lateral Raise",
        still_public=False,
        shared=False,
        cache_stale=True,
    )
    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED
    assert await t.media.approved_for(t.exercise) == []
    assert not t.is_public()
    assert t.private.objects == {t.quarantined: b"png-bytes"}
    assert t.private.content_types[t.quarantined] == "image/png"


async def test_removed_page_lists_when_and_from_what_without_a_public_url() -> None:
    t = _Takedown()
    await t.review.remove(t.link)

    page = await t.review.page(MediaLinkStatus.REMOVED)

    [group] = page.groups
    [picture] = group.pictures
    assert (picture.status, picture.removed_at, picture.removed_from) == (
        MediaLinkStatus.REMOVED,
        REVIEW_NOW,
        MediaLinkStatus.APPROVED,
    )
    assert picture.public_url is None
    assert page.counts[MediaLinkStatus.REMOVED] == 1


@pytest.mark.parametrize("status", [MediaLinkStatus.APPROVED, MediaLinkStatus.SUGGESTED])
async def test_restore_moves_object_back_and_restores_earlier_status(
    status: MediaLinkStatus,
) -> None:
    t = _Takedown(status)
    await t.review.remove(t.link)

    outcome = await t.review.restore(t.link)

    assert outcome == Restored(exercise_name="Dumbbell Lateral Raise", status=status)
    assert t.media.status_of(t.link) is status
    assert t.public.objects == {t.key: b"png-bytes"}
    assert t.public.content_types[t.key] == "image/png"
    assert t.private.objects == {}


@pytest.mark.parametrize("failing", ["get", "put", "delete"])
async def test_remove_keeps_link_removed_when_move_fails(failing: ObjectStorageOperation) -> None:
    t = _Takedown()
    bucket = t.private if failing == "put" else t.public
    bucket.failing.add(failing)

    outcome = await t.review.remove(t.link)

    assert outcome == Removed(
        exercise_name="Dumbbell Lateral Raise", still_public=True, shared=False
    )
    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED
    assert t.is_public()


async def test_remove_without_buckets_hides_the_link_and_says_the_file_is_still_public() -> None:
    t = _Takedown(buckets=False)

    outcome = await t.review.remove(t.link)

    assert isinstance(outcome, Removed)
    assert outcome.still_public
    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED


async def test_remove_again_retries_the_move_and_keeps_the_first_removal() -> None:
    t = _Takedown()
    t.public.failing.add("delete")
    await t.review.remove(t.link)
    t.public.failing.clear()

    outcome = await t.review.remove(t.link)

    assert isinstance(outcome, Removed)
    assert not outcome.still_public
    assert not t.is_public()
    [picture] = (await t.review.page(MediaLinkStatus.REMOVED)).groups[0].pictures
    assert picture.removed_from is MediaLinkStatus.APPROVED


async def test_remove_of_an_object_already_moved_succeeds() -> None:
    t = _Takedown()
    await t.review.remove(t.link)

    outcome = await t.review.remove(t.link)

    assert isinstance(outcome, Removed)
    assert not outcome.still_public
    assert t.private.objects == {t.quarantined: b"png-bytes"}


async def test_remove_leaves_object_public_while_another_link_shows_it() -> None:
    t = _Takedown()
    other = t.media.exercise("Cable Lateral Raise")
    shown = t.media.share(other, t.link)

    outcome = await t.review.remove(t.link)

    assert outcome == Removed(
        exercise_name="Dumbbell Lateral Raise", still_public=False, shared=True
    )
    assert t.is_public()
    assert t.media.status_of(shown) is MediaLinkStatus.APPROVED


async def test_remove_of_embedded_media_only_hides_the_link() -> None:
    t = _Takedown(asset=embedded_asset())

    outcome = await t.review.remove(t.link)

    assert outcome == Removed(
        exercise_name="Dumbbell Lateral Raise", still_public=False, shared=False
    )
    assert t.private.objects == {}
    assert await t.review.restore(t.link) == Restored(
        exercise_name="Dumbbell Lateral Raise", status=MediaLinkStatus.APPROVED
    )


@pytest.mark.parametrize("failing", ["get", "put"])
async def test_restore_leaves_the_link_removed_when_the_move_back_fails(
    failing: ObjectStorageOperation,
) -> None:
    t = _Takedown()
    await t.review.remove(t.link)
    bucket = t.private if failing == "get" else t.public
    bucket.failing.add(failing)

    outcome = await t.review.restore(t.link)

    assert outcome == RestoreFailed(exercise_name="Dumbbell Lateral Raise")
    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED
    assert not t.is_public()


async def test_restore_whose_quarantine_copy_cannot_be_deleted_still_restores() -> None:
    t = _Takedown()
    await t.review.remove(t.link)
    t.private.failing.add("delete")

    outcome = await t.review.restore(t.link)

    assert isinstance(outcome, Restored)
    assert t.is_public()


async def test_restore_of_a_file_that_never_left_the_public_bucket() -> None:
    t = _Takedown()
    t.public.failing.add("delete")
    await t.review.remove(t.link)
    t.public.failing.clear()
    t.private.objects.clear()

    outcome = await t.review.restore(t.link)

    assert isinstance(outcome, Restored)
    assert t.is_public()


async def test_restore_of_a_file_gone_from_both_buckets_fails() -> None:
    t = _Takedown()
    await t.review.remove(t.link)
    t.private.objects.clear()

    assert await t.review.restore(t.link) == RestoreFailed(exercise_name="Dumbbell Lateral Raise")
    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED


async def test_restore_without_buckets_fails_for_stored_media() -> None:
    t = _Takedown(MediaLinkStatus.REMOVED, buckets=False)

    assert isinstance(await t.review.restore(t.link), RestoreFailed)


async def test_restore_of_a_link_that_is_not_removed_says_its_status() -> None:
    t = _Takedown(MediaLinkStatus.SUGGESTED)

    assert await t.review.restore(t.link) == Restored(
        exercise_name="Dumbbell Lateral Raise", status=MediaLinkStatus.SUGGESTED
    )
    assert t.is_public()


class _RestoredInAnotherTab(MemoryExerciseMedia):
    """Another tab's Restore lands between the review's read and its own update."""

    async def restore(self, link_id: UUID) -> MediaLinkStatus | None:
        await super().restore(link_id)
        return None


async def test_restore_that_another_tab_got_to_first_says_the_status_now() -> None:
    t = _Takedown(media=_RestoredInAnotherTab())
    await t.review.remove(t.link)

    assert await t.review.restore(t.link) == Restored(
        exercise_name="Dumbbell Lateral Raise", status=MediaLinkStatus.APPROVED
    )
    assert t.is_public()


class _RestoredAroundTheRemove(MemoryExerciseMedia):
    """A stale tab posts Remove on a removed link, and another tab's Restore lands around its
    update, so the update changes nothing and the link is live again (#556 review)."""

    async def remove(self, link_id: UUID, *, at: datetime) -> MediaLinkStatus | None:
        await super().restore(link_id)
        return None


async def test_remove_that_changed_nothing_on_a_live_link_leaves_its_file_public() -> None:
    t = _Takedown(media=_RestoredAroundTheRemove())
    t.media.links[0].status = MediaLinkStatus.REMOVED
    t.media.links[0].removed_from = MediaLinkStatus.APPROVED

    outcome = await t.review.remove(t.link)

    assert outcome == Restored(
        exercise_name="Dumbbell Lateral Raise", status=MediaLinkStatus.APPROVED
    )
    assert t.media.status_of(t.link) is MediaLinkStatus.APPROVED
    assert t.is_public()
    assert t.private.objects == {}


class _RestoreThatFails(MemoryExerciseMedia):
    async def restore(self, link_id: UUID) -> MediaLinkStatus | None:
        raise MediaRepositoryError


async def test_restore_whose_status_update_fails_hides_the_file_again() -> None:
    t = _Takedown(media=_RestoreThatFails())
    await t.review.remove(t.link)

    with pytest.raises(MediaRepositoryError):
        await t.review.restore(t.link)

    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED
    assert not t.is_public()
    assert t.quarantined in t.private.objects


async def test_remove_and_restore_of_an_unknown_link_are_none() -> None:
    review = memory_exercise_media_review()
    unknown: UUID = uuid4()

    assert await review.remove(unknown) is None
    assert await review.restore(unknown) is None


async def test_remove_purges_the_pictures_public_url() -> None:
    t = _Takedown(cdn=FakeCdn())
    assert t.cdn is not None

    outcome = await t.review.remove(t.link)

    assert t.cdn.purged == [f"https://media.example.org/{t.key}"]
    assert outcome == Removed(
        exercise_name="Dumbbell Lateral Raise", still_public=False, shared=False
    )


async def test_remove_does_not_purge_a_shared_embedded_unmoved_or_unlinked_picture() -> None:
    shared = _Takedown(cdn=FakeCdn())
    shared.media.share(shared.media.exercise("Cable Lateral Raise"), shared.link)
    await shared.review.remove(shared.link)
    embedded = _Takedown(asset=embedded_asset(), cdn=FakeCdn())
    await embedded.review.remove(embedded.link)
    stuck = _Takedown(cdn=FakeCdn())
    stuck.public.failing.add("delete")
    await stuck.review.remove(stuck.link)
    no_base = _Takedown(cdn=FakeCdn(), base_url=None)
    await no_base.review.remove(no_base.link)

    for case in (shared, embedded, stuck, no_base):
        assert case.cdn is not None
        assert case.cdn.purged == []


async def test_restore_never_purges() -> None:
    t = _Takedown(cdn=FakeCdn())
    assert t.cdn is not None
    await t.review.remove(t.link)
    t.cdn.purged.clear()

    await t.review.restore(t.link)

    assert t.cdn.purged == []


async def test_remove_with_a_failed_purge_stays_removed_and_flags_the_cache(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = _Takedown(cdn=FakeCdn(failing=True))

    outcome = await t.review.remove(t.link)

    assert outcome == Removed(
        exercise_name="Dumbbell Lateral Raise",
        still_public=False,
        shared=False,
        cache_stale=True,
    )
    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED
    assert not t.is_public()
    assert "cache purge refused: HTTP 403" in caplog.text


async def test_remove_with_unset_cdn_skips_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    t = _Takedown()

    with caplog.at_level("INFO"):
        outcome = await t.review.remove(t.link)

    assert isinstance(outcome, Removed)
    # Nothing purged the cache, so the page must still warn that a cached copy may show.
    assert outcome.cache_stale
    assert "cache purge skipped" in caplog.text
