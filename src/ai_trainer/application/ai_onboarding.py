"""The AI onboarding (PRD-0007 B33-B36, F20, F21, ADR-0028): a sport pick, prose answers read in
the background, and a summary the athlete confirms. A send stores its answer and returns at once;
only the summary waits for reads, and only confirm writes the profile (invariants 1-4)."""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.application.onboarding import timezone_names
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingAnswersPort, OnboardingReaderPort
from ai_trainer.domain.athlete_profile import (
    AboutYou,
    FieldProblem,
    about_you_from_read,
    parse_about_you,
    profile_from,
)
from ai_trainer.domain.onboarding import (
    MAX_ANSWER_LENGTH,
    AnswerStatus,
    OnboardingAnswer,
    OnboardingStep,
)
from ai_trainer.domain.sports.registry import SportRegistry

logger = logging.getLogger(__name__)

_FALLBACK_TIMEZONE = "UTC"
_UNREAD = (AnswerStatus.PENDING, AnswerStatus.FAILED)


class OnboardingCard(StrEnum):
    """The card a reload resumes at, derived from what the server holds (ADR-0028)."""

    SPORT = "sport"
    ABOUT_YOU = "about_you"
    SUMMARY = "summary"


class EmptyAnswerError(Exception):
    """Raised when Send carries no text; Skip is how a step is left empty."""


class InvalidSummaryError(Exception):
    """Raised when a summary field is out of its bounds; `problems` names every one."""

    def __init__(self, problems: Mapping[str, FieldProblem]) -> None:
        super().__init__(f"invalid summary fields {sorted(problems)}")
        self.problems = dict(problems)


@dataclass(frozen=True)
class OnboardingSummary:
    """What the summary shows to edit (B35): the About you fields, whether that step was
    skipped, and the timezone to correct (B36)."""

    about_you: AboutYou
    about_you_skipped: bool
    timezone: str


async def pick_sport(
    user_id: UUID,
    sport_id: str,
    raw_timezone: str,
    *,
    registry: SportRegistry,
    answers: OnboardingAnswersPort,
) -> None:
    """Starts onboarding with one sport (B36) and the browser's zone. An unknown sport raises
    `UnknownSportError` and stores nothing; a zone that isn't an IANA name falls back to UTC,
    since the summary shows it to correct."""
    registry.get(sport_id)
    zone = raw_timezone.strip()
    await answers.start(user_id, sport_id, zone if zone in timezone_names() else _FALLBACK_TIMEZONE)


async def current_card(user_id: UUID, *, answers: OnboardingAnswersPort) -> OnboardingCard:
    if not await answers.list_sports(user_id):
        return OnboardingCard.SPORT
    stored = await answers.list_answers(user_id)
    if not any(answer.step is OnboardingStep.ABOUT_YOU for answer in stored):
        return OnboardingCard.ABOUT_YOU
    return OnboardingCard.SUMMARY


async def answer_of(
    user_id: UUID, step: OnboardingStep, *, answers: OnboardingAnswersPort
) -> OnboardingAnswer | None:
    """The step's stored answer, to prefill its card after Back."""
    return next((a for a in await answers.list_answers(user_id) if a.step is step), None)


async def send_answer(
    user_id: UUID,
    step: OnboardingStep,
    text: str,
    *,
    answers: OnboardingAnswersPort,
    reads: BackgroundReads,
    reader: OnboardingReaderPort,
    sport_id: str,
    timezone: str,
) -> None:
    """Stores the answer as `pending` and starts its read in the background, never awaiting
    it (ADR-0028 invariant 2). The text is trimmed and cut to the column's length."""
    # Postgres text cannot hold NUL; only a hand-crafted request sends one.
    cleaned = text.replace("\x00", "").strip()[:MAX_ANSWER_LENGTH].strip()
    if not cleaned:
        raise EmptyAnswerError
    revision = await answers.store_answer(user_id, step, None, cleaned)
    answer = OnboardingAnswer(
        step=step, sport_id=None, text=cleaned, revision=revision, status=AnswerStatus.PENDING
    )
    reads.start(
        (user_id, step, None),
        read_answer(
            user_id, answer, answers=answers, reader=reader, sport_id=sport_id, timezone=timezone
        ),
    )


async def skip_step(user_id: UUID, step: OnboardingStep, *, answers: OnboardingAnswersPort) -> None:
    """Marks the step skipped; it saves nothing and starts no read (B36)."""
    await answers.skip_answer(user_id, step, None)


async def read_answer(
    user_id: UUID,
    answer: OnboardingAnswer,
    *,
    answers: OnboardingAnswersPort,
    reader: OnboardingReaderPort,
    sport_id: str,
    timezone: str,
) -> None:
    """Reads one answer and stores the result under its revision, so a stale read is dropped
    (ADR-0028 invariant 3). A failed read marks the answer for the summary to read again."""
    try:
        parsed = await reader.read(user_id, answer, sport_id=sport_id, timezone=timezone)
    except Exception:
        logger.exception("onboarding read raised")
        parsed = None
    if parsed is None:
        await answers.mark_failed(user_id, answer.step, answer.sport_id, answer.revision)
        return
    await answers.save_read(user_id, answer.step, answer.sport_id, answer.revision, parsed)


async def catch_up(
    user_id: UUID,
    *,
    answers: OnboardingAnswersPort,
    reads: BackgroundReads,
    reader: OnboardingReaderPort,
    timezone: str,
) -> None:
    """Finishes every unread answer before the summary shows (B35): it waits for a read this
    process still runs, then reads each answer still pending or failed once itself. So a read
    lost to a restart costs only time, and one that fails twice leaves its fields empty."""
    for answer in await answers.list_answers(user_id):
        if answer.status in _UNREAD and (
            task := reads.running((user_id, answer.step, answer.sport_id))
        ):
            # A stream closed by the browser must not cancel the held read with it.
            await asyncio.shield(task)
    sports = await answers.list_sports(user_id)
    if not sports:
        return
    for answer in await answers.list_answers(user_id):
        if answer.status in _UNREAD:
            await read_answer(
                user_id,
                answer,
                answers=answers,
                reader=reader,
                sport_id=sports[0],
                timezone=timezone,
            )


async def summary_of(
    user_id: UUID, *, answers: OnboardingAnswersPort, timezone: str
) -> OnboardingSummary:
    about_you = await answer_of(user_id, OnboardingStep.ABOUT_YOU, answers=answers)
    return OnboardingSummary(
        about_you=about_you_from_read(about_you.parsed or {})
        if about_you is not None
        else AboutYou(),
        about_you_skipped=about_you is not None and about_you.status is AnswerStatus.SKIPPED,
        timezone=timezone,
    )


async def confirm_summary(
    user_id: UUID,
    form: Mapping[str, str],
    *,
    answers: OnboardingAnswersPort,
    clock: ClockPort,
) -> None:
    """Validates the edited summary and, in one transaction, writes the profile, the sports,
    the timezone and `onboarded_at`, and deletes the answers (B35, B36). The age becomes a
    birth year in the athlete's own year. Any bad field raises `InvalidSummaryError` and
    writes nothing."""
    zone = form.get("timezone", "").strip()
    parsed = parse_about_you(form)
    problems: dict[str, FieldProblem] = dict(parsed) if isinstance(parsed, dict) else {}
    if zone not in timezone_names():
        problems["timezone"] = FieldProblem.UNKNOWN
    if problems or isinstance(parsed, dict):
        raise InvalidSummaryError(problems)
    now = clock.now()
    sports: Sequence[str] = await answers.list_sports(user_id)
    await answers.confirm(
        user_id,
        profile=profile_from(parsed, today=now.astimezone(ZoneInfo(zone)).date()),
        sport_ids=list(sports),
        timezone=zone,
        when=now,
    )
