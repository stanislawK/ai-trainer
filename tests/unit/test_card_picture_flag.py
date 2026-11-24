"""An athlete's "Looks wrong" on a picture drawn on the exercise card (ADR-0031 *Picture on the
card*, *Flag*, #644): it takes the link down through the admin's takedown path (the link
`removed` from `approved`, the object moved to `quarantine/<key>` in the uploads bucket, its public
URL purged) and records one content-free `picture_flagged` event. A wger picture, an
admin-approved generated one and a picture already flagged are left as they are, and record
nothing."""

from uuid import UUID

import pytest

from ai_trainer.application.exercise_card_picture_flag import CardPictureFlag
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.media_quarantine import MediaQuarantine, MediaTakedown
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.media import MediaAsset, MediaLinkStatus
from tests.chat_support import FixedClock
from tests.exercise_media_support import (
    REVIEW_NOW,
    FakeCdn,
    MemoryExerciseMedia,
    ReviewClock,
    card_drawn_asset,
    generated_asset,
    wger_asset,
)
from tests.feedback_support import InMemoryFeedbackEvents
from tests.knowledge_support import MemoryStorage

BASE = "https://media.example.org"
ATHLETE = UUID("00000000-0000-4000-8000-000000000644")


class _Flag:
    def __init__(self, asset: MediaAsset | None = None) -> None:
        self.media = MemoryExerciseMedia()
        self.exercise = self.media.exercise("Dumbbell Front Raise")
        self.asset = asset or card_drawn_asset()
        self.link = self.media.put(self.exercise, self.asset, MediaLinkStatus.APPROVED)
        self.public, self.private = MemoryStorage(), MemoryStorage()
        assert self.asset.storage_key is not None
        self.key = self.asset.storage_key
        self.public.objects[self.key] = b"jpeg-bytes"
        self.public.content_types[self.key] = "image/jpeg"
        self.cdn = FakeCdn()
        self.events = InMemoryFeedbackEvents()
        self.flag = CardPictureFlag(
            media=self.media,
            takedown=MediaTakedown(
                media=self.media,
                quarantine=MediaQuarantine(media=self.public, uploads=self.private),
                clock=ReviewClock(),
                public_base_url=BASE,
                cdn=self.cdn,
            ),
            feedback=FeedbackRecorder(self.events, clock=FixedClock()),
        )

    async def run(self) -> bool:
        return await self.flag.flag(user_id=ATHLETE, exercise_id=self.exercise)


# AC 2: its public URL stops serving it.
async def test_flag_moves_the_object_to_quarantine_and_purges_its_url() -> None:
    t = _Flag()

    assert await t.run()

    assert t.key not in t.public.objects
    assert t.private.objects == {f"quarantine/{t.key}": b"jpeg-bytes"}
    assert t.private.content_types[f"quarantine/{t.key}"] == "image/jpeg"
    assert t.cdn.purged == [f"{BASE}/{t.key}"]
    [row] = await t.media.review_links(MediaLinkStatus.REMOVED, exercise_id=t.exercise)
    assert (row.removed_from, row.removed_at) == (MediaLinkStatus.APPROVED, REVIEW_NOW)


# AC 3: one content-free event, with no template and no field.
async def test_flag_records_one_picture_flagged_event_without_template_or_fields() -> None:
    t = _Flag()

    await t.run()

    [event] = t.events.events
    assert (event.user_id, event.kind) == (ATHLETE, FeedbackKind.PICTURE_FLAGGED)
    assert (event.template_id, event.template_version, tuple(event.fields)) == (None, None, ())


async def test_a_second_flag_changes_nothing_and_records_nothing() -> None:
    t = _Flag()
    await t.run()
    t.cdn.purged.clear()

    assert not await t.run()

    assert len(t.events.events) == 1
    assert t.cdn.purged == []


@pytest.mark.parametrize(
    "asset",
    [wger_asset(), wger_asset(ai=True), generated_asset()],
    ids=["wger", "wger-ai", "admin"],
)
async def test_a_picture_not_drawn_on_the_card_is_left_as_it_is(asset: MediaAsset) -> None:
    t = _Flag(asset)

    assert not await t.run()

    assert t.media.status_of(t.link) is MediaLinkStatus.APPROVED
    assert t.key in t.public.objects
    assert t.private.objects == {}
    assert t.cdn.purged == []
    assert t.events.events == []


async def test_a_card_picture_behind_the_shown_one_is_not_taken_down() -> None:
    """Only the picture the card shows (the first approved stored one) can be flagged."""
    t = _Flag(wger_asset())
    drawn = t.media.put(t.exercise, card_drawn_asset(), MediaLinkStatus.APPROVED)

    assert not await t.run()

    assert t.media.status_of(drawn) is MediaLinkStatus.APPROVED
    assert t.events.events == []


async def test_an_exercise_without_an_approved_picture_records_nothing() -> None:
    t = _Flag()
    other = t.media.exercise("Pull-Up")

    assert not await t.flag.flag(user_id=ATHLETE, exercise_id=other)

    assert t.events.events == []


async def test_a_flag_whose_file_cannot_move_still_hides_the_link_and_records_it() -> None:
    """The link is `removed` first, so the card stops showing it whatever the bucket does."""
    t = _Flag()
    t.public.failing.add("delete")

    assert await t.run()

    assert t.media.status_of(t.link) is MediaLinkStatus.REMOVED
    assert [event.kind for event in t.events.events] == [FeedbackKind.PICTURE_FLAGGED]


async def test_a_tap_that_loses_the_race_records_nothing() -> None:
    """Two taps at once: both read the picture as shown, only the first one moves the link."""
    t = _Flag()
    [shown] = await t.media.review_links(MediaLinkStatus.APPROVED, exercise_id=t.exercise)
    await t.run()

    async def stale(status: MediaLinkStatus, *, exercise_id: UUID) -> list[object]:
        return [shown]

    t.media.review_links = stale  # type: ignore[method-assign,assignment]

    assert not await t.run()

    assert [event.kind for event in t.events.events] == [FeedbackKind.PICTURE_FLAGGED]
