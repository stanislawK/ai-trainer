"""The admin's F24 review of exercise media (ADR-0031 *Media links*, #554): pictures grouped
per exercise, their credit and public URL, one Approve, and Approve all from wger only."""

from uuid import uuid4

from ai_trainer.application.exercise_media_review import (
    Approved,
    BulkApproved,
    NotSuggested,
    WgerCredit,
)
from ai_trainer.domain.media import MediaLinkStatus
from tests.exercise_media_support import (
    MemoryExerciseMedia,
    embedded_asset,
    generated_asset,
    memory_exercise_media_review,
    wger_asset,
)

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
    review = memory_exercise_media_review(media)

    assert await review.approve_all_from_wger(row) == BulkApproved(count=2, generated_left=1)
    assert media.status_of(generated) is MediaLinkStatus.SUGGESTED
    assert await review.approve_all_from_wger(row) == BulkApproved(count=0, generated_left=1)
