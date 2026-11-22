"""Writing one exercise's beginner how-to (ADR-0031 *Beginner how-to*, F23, #589, #591).

The text is written from the catalogue exercise's public English text only, under the athlete
whose card asked for it: one `llm_calls` row, within their budget (ADR-0018, ADR-0029). A row
written from the same instructions, or edited by an admin, stands, and no model is called. A
timeout, an error, a budget refusal, the model's own refusal or a text that fails the code checks
stores nothing; the log line names only the reason and the exercise ID.

A text that passes the code checks is checked by Jev, under the same athlete and budget, before
it is stored (#591). It fails closed: a failed check, a timeout or an error stores no text and
counts; the second failed check of the same source stores the row `rejected`, after which later
cards make no calls. A check the budget refuses never ran, so it stores and counts nothing."""

import logging
from uuid import UUID

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.exercise_simple_texts import (
    ExerciseSimpleTextsPort,
    SimpleHowtoCheckPort,
    SimpleHowtoWriterPort,
)
from ai_trainer.domain.exercise_simple_texts import (
    SIMPLE_TEXT_LANG,
    HowtoCallFailed,
    HowtoCheckRefused,
    HowtoFailed,
    HowtoFailure,
    HowtoPresent,
    HowtoRejected,
    HowtoResult,
    HowtoSkipped,
    HowtoSource,
    HowtoStored,
    NewSimpleText,
    NoHowto,
    SimpleHowtoDraft,
    SimpleTextStatus,
    invalid_reason,
    needs_writing,
    source_sha256,
)

logger = logging.getLogger(__name__)


class WriteSimpleHowto:
    def __init__(
        self,
        *,
        texts: ExerciseSimpleTextsPort,
        writer: SimpleHowtoWriterPort,
        check: SimpleHowtoCheckPort,
        clock: ClockPort,
        max_chars: int,
    ) -> None:
        self._texts = texts
        self._writer = writer
        self._check = check
        self._clock = clock
        self._max_chars = max_chars

    async def write(self, *, user_id: UUID, exercise_id: UUID) -> HowtoResult:
        source = await self._texts.howto_source(exercise_id, lang=SIMPLE_TEXT_LANG)
        if source is None:
            return HowtoSkipped(reason="no_english_text")
        sha256 = source_sha256(source.instructions)
        stored = await self._texts.stored(exercise_id, lang=SIMPLE_TEXT_LANG)
        if stored is not None and not needs_writing(stored, sha256):
            # Only a row with text gets here, so a `rejected` one is current and checked twice.
            # An admin's edit is a human's text, whatever status the row still carries.
            if stored.status is SimpleTextStatus.REJECTED and stored.edited_at is None:
                return HowtoRejected()
            return HowtoPresent()
        deps = source.deps(max_chars=self._max_chars)
        draft = await self._writer.write(user_id=user_id, deps=deps)
        if isinstance(draft, HowtoCallFailed):
            return self._failed(draft.reason, exercise_id)
        if isinstance(draft, NoHowto):
            return self._failed("refused", exercise_id)
        if reason := invalid_reason(draft.text, deps, max_chars=self._max_chars):
            return self._failed(reason, exercise_id)
        return await self._checked(user_id, source, draft)

    async def _checked(
        self, user_id: UUID, source: HowtoSource, draft: SimpleHowtoDraft
    ) -> HowtoResult:
        text = draft.text.strip()
        check = await self._check.check(user_id=user_id, original=source.instructions, summary=text)
        if isinstance(check, HowtoCheckRefused):
            return self._failed("rate_limited", source.exercise_id)
        new = NewSimpleText(
            exercise_id=source.exercise_id,
            lang=SIMPLE_TEXT_LANG,
            text=text,
            template_id=draft.template_id,
            template_version=draft.template_version,
            model=draft.model,
            source_sha256=source_sha256(source.instructions),
            check_version=check.version,
            created_at=self._clock.now(),
        )
        if check.passed:
            return HowtoStored(text=text) if await self._texts.write(new) else HowtoPresent()
        match await self._texts.record_failed_check(new):
            case "counted":
                return self._failed("check_failed", source.exercise_id)
            case "rejected":
                logger.info("simple how-to rejected: exercise=%s", source.exercise_id)
                return HowtoRejected()
            case "present":
                return HowtoPresent()

    @staticmethod
    def _failed(reason: HowtoFailure, exercise_id: UUID) -> HowtoFailed:
        logger.info("simple how-to not stored: reason=%s exercise=%s", reason, exercise_id)
        return HowtoFailed(reason=reason)
