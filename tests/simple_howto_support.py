"""Fakes for the beginner how-to (ADR-0031 *Beginner how-to*, #589, #591, #592): the shared
texts table with the repository's conditional upserts, a scripted writer and a scripted Jev check,
for the service's unit tests and the exercise card's route tests."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from ai_trainer.domain.exercise_simple_texts import (
    CardHowtoSource,
    FailedCheckOutcome,
    HowtoCallFailed,
    HowtoCheck,
    HowtoCheckRefused,
    HowtoSource,
    NewSimpleText,
    NoHowto,
    SimpleHowtoDeps,
    SimpleHowtoDraft,
    SimpleTextStatus,
    StoredSimpleText,
    needs_writing,
)

NOW = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
CHECK_VERSION = 1


class FrozenClock:
    def now(self) -> datetime:
        return NOW


@dataclass
class StoredRow:
    text: NewSimpleText
    body: str | None
    status: SimpleTextStatus = SimpleTextStatus.SHOWN
    check_failures: int = 0


@dataclass
class InMemorySimpleTexts:
    """Implements `ExerciseSimpleTextsPort` with the repository's conditional upserts."""

    sources: dict[UUID, HowtoSource] = field(default_factory=dict)
    rows: dict[UUID, StoredRow] = field(default_factory=dict)
    edited: set[UUID] = field(default_factory=set)
    writes: list[NewSimpleText] = field(default_factory=list)
    failed_checks: list[NewSimpleText] = field(default_factory=list)
    card_reads: list[set[UUID]] = field(default_factory=list)

    async def howto_source(self, exercise_id: UUID, *, lang: str) -> HowtoSource | None:
        assert lang == "en"
        return self.sources.get(exercise_id)

    async def stored(self, exercise_id: UUID, *, lang: str) -> StoredSimpleText | None:
        row = self.rows.get(exercise_id)
        if row is None:
            return None
        return StoredSimpleText(
            source_sha256=row.text.source_sha256,
            status=row.status,
            edited_at=NOW if exercise_id in self.edited else None,
            has_text=row.body is not None,
            check_failures=row.check_failures,
        )

    async def card_rows(
        self, exercise_ids: Collection[UUID], *, lang: str
    ) -> Mapping[UUID, CardHowtoSource]:
        assert lang == "en"
        self.card_reads.append(set(exercise_ids))
        found: dict[UUID, CardHowtoSource] = {}
        for exercise_id in exercise_ids:
            source = self.sources.get(exercise_id)
            if source is None:
                continue
            row = self.rows.get(exercise_id)
            found[exercise_id] = CardHowtoSource(
                instructions=source.instructions,
                stored=await self.stored(exercise_id, lang=lang),
                text=None if row is None else row.body,
            )
        return found

    async def write(self, text: NewSimpleText) -> bool:
        stored = await self.stored(text.exercise_id, lang=text.lang)
        if stored is not None and not needs_writing(stored, text.source_sha256):
            return False
        failures = (
            stored.check_failures
            if stored is not None and stored.source_sha256 == text.source_sha256
            else 0
        )
        self.rows[text.exercise_id] = StoredRow(text, text.text, check_failures=failures)
        self.writes.append(text)
        return True

    async def record_failed_check(self, text: NewSimpleText) -> FailedCheckOutcome:
        self.failed_checks.append(text)
        stored = await self.stored(text.exercise_id, lang=text.lang)
        if stored is not None and not needs_writing(stored, text.source_sha256):
            return "present"
        if stored is not None and stored.source_sha256 == text.source_sha256:
            self.rows[text.exercise_id] = StoredRow(
                text, text.text, SimpleTextStatus.REJECTED, stored.check_failures + 1
            )
            return "rejected"
        self.rows[text.exercise_id] = StoredRow(text, None, check_failures=1)
        return "counted"


@dataclass
class ScriptedWriter:
    """Implements `SimpleHowtoWriterPort`; returns `reply` and remembers each call."""

    reply: SimpleHowtoDraft | NoHowto | HowtoCallFailed
    calls: list[tuple[UUID, SimpleHowtoDeps]] = field(default_factory=list)

    async def write(
        self, *, user_id: UUID, deps: SimpleHowtoDeps
    ) -> SimpleHowtoDraft | NoHowto | HowtoCallFailed:
        self.calls.append((user_id, deps))
        return self.reply


@dataclass
class ScriptedCheck:
    """Implements `SimpleHowtoCheckPort`; returns the next of `replies` (the last repeats)."""

    replies: list[HowtoCheck | HowtoCheckRefused] = field(
        default_factory=lambda: [HowtoCheck(passed=True, version=CHECK_VERSION)]
    )
    calls: list[tuple[UUID, str, str]] = field(default_factory=list)

    async def check(
        self, *, user_id: UUID, original: str, summary: str
    ) -> HowtoCheck | HowtoCheckRefused:
        self.calls.append((user_id, original, summary))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]
