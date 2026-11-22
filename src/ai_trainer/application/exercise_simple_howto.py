"""Writing one exercise's beginner how-to (ADR-0031 *Beginner how-to*, F23, #589).

The text is written from the catalogue exercise's public English text only, under the athlete
whose card asked for it: one `llm_calls` row, within their budget (ADR-0018, ADR-0029). A row
written from the same instructions, or edited by an admin, stands, and no model is called. A
timeout, an error, a budget refusal, the model's own refusal or a text that fails the code checks
stores nothing; the log line names only the reason and the exercise ID."""

import logging
from uuid import UUID

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.exercise_simple_texts import (
    ExerciseSimpleTextsPort,
    SimpleHowtoWriterPort,
)
from ai_trainer.domain.exercise_simple_texts import (
    SIMPLE_TEXT_LANG,
    HowtoCallFailed,
    HowtoFailed,
    HowtoFailure,
    HowtoPresent,
    HowtoResult,
    HowtoSkipped,
    HowtoStored,
    NewSimpleText,
    NoHowto,
    invalid_reason,
    is_current,
    source_sha256,
)

logger = logging.getLogger(__name__)


class WriteSimpleHowto:
    def __init__(
        self,
        *,
        texts: ExerciseSimpleTextsPort,
        writer: SimpleHowtoWriterPort,
        clock: ClockPort,
        max_chars: int,
    ) -> None:
        self._texts = texts
        self._writer = writer
        self._clock = clock
        self._max_chars = max_chars

    async def write(self, *, user_id: UUID, exercise_id: UUID) -> HowtoResult:
        source = await self._texts.howto_source(exercise_id, lang=SIMPLE_TEXT_LANG)
        if source is None:
            return HowtoSkipped(reason="no_english_text")
        sha256 = source_sha256(source.instructions)
        stored = await self._texts.stored(exercise_id, lang=SIMPLE_TEXT_LANG)
        if stored is not None and is_current(stored, sha256):
            return HowtoPresent()
        deps = source.deps()
        draft = await self._writer.write(user_id=user_id, deps=deps)
        if isinstance(draft, HowtoCallFailed):
            return self._failed(draft.reason, exercise_id)
        if isinstance(draft, NoHowto):
            return self._failed("refused", exercise_id)
        if reason := invalid_reason(draft.text, deps, max_chars=self._max_chars):
            return self._failed(reason, exercise_id)
        text = draft.text.strip()
        written = await self._texts.write(
            NewSimpleText(
                exercise_id=exercise_id,
                lang=SIMPLE_TEXT_LANG,
                text=text,
                template_id=draft.template_id,
                template_version=draft.template_version,
                model=draft.model,
                source_sha256=sha256,
                created_at=self._clock.now(),
            )
        )
        return HowtoStored(text=text) if written else HowtoPresent()

    @staticmethod
    def _failed(reason: HowtoFailure, exercise_id: UUID) -> HowtoFailed:
        logger.info("simple how-to not stored: reason=%s exercise=%s", reason, exercise_id)
        return HowtoFailed(reason=reason)
