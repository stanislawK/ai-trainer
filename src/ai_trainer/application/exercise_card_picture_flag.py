"""An athlete's "Looks wrong" on a picture drawn on the exercise card (ADR-0031 *Picture on the
card*, *Flag*; PRD 0013 F24; #644).

Only the picture the card shows can be flagged, and only when the card drew it (the `#card`
marker #643 writes): an admin-approved picture, generated or wger, has no flag. One tap takes the
link down through the admin's takedown path (`MediaTakedown`), so it is hidden for every athlete
at once and, being a removed generated picture, never drawn again; Restore at `/admin/exercises`
is the way back. Only the tap that moved the link records one content-free `picture_flagged`
event, with no template and no field. The log line names the exercise ID only."""

import logging
from uuid import UUID

from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.media_credit import drawn_on_card
from ai_trainer.application.media_quarantine import MediaTakedown
from ai_trainer.application.ports.media import ExerciseMediaReviewPort
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.media import MediaLinkStatus

logger = logging.getLogger(__name__)


class CardPictureFlag:
    """Facade for the chat's flag route. Catalogue media holds no user data; the exercise ID
    comes from the athlete's own, user-scoped question."""

    def __init__(
        self,
        *,
        media: ExerciseMediaReviewPort,
        takedown: MediaTakedown,
        feedback: FeedbackRecorder,
    ) -> None:
        self._media = media
        self._takedown = takedown
        self._feedback = feedback

    async def flag(self, *, user_id: UUID, exercise_id: UUID) -> bool:
        """True when this tap took the card's drawn picture down. A picture the card didn't
        draw, one already flagged, or no picture at all changes nothing and records nothing."""
        approved = await self._media.review_links(MediaLinkStatus.APPROVED, exercise_id=exercise_id)
        # The card shows the first approved stored picture by position (#558).
        shown = next((row for row in approved if row.asset.storage_key is not None), None)
        if shown is None or not drawn_on_card(shown.asset):
            return False
        result = await self._takedown.take_down(shown)
        if result.removed_from is None:
            return False
        await self._feedback.record(user_id, FeedbackKind.PICTURE_FLAGGED)
        logger.info("card picture flagged: exercise=%s", exercise_id)
        return True
