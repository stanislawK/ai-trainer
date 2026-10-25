"""The AI onboarding (PRD 0008 B33-B36, F20, F21, ADR-0028): a sport pick, About you, then goals,
level and schedule for the sport, another sport's three steps if the athlete adds one (#256),
prose answers read in the background, and a summary the athlete confirms. A send stores its
answer and returns at once; only the summary waits for reads, and only confirm writes the
profile (invariants 1-4)."""

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
from ai_trainer.domain.sport_profile import (
    SportForm,
    SportProfile,
    SportSection,
    parse_sport_section,
    sport_from_read,
)
from ai_trainer.domain.sports.registry import SportRegistry

logger = logging.getLogger(__name__)

_FALLBACK_TIMEZONE = "UTC"
_UNREAD = (AnswerStatus.PENDING, AnswerStatus.FAILED)


class OnboardingCard(StrEnum):
    """The card a reload resumes at, derived from what the server holds (ADR-0028)."""

    SPORT = "sport"
    ABOUT_YOU = "about_you"
    GOALS = "goals"
    LEVEL = "level"
    SCHEDULE = "schedule"
    MORE = "more"
    SUMMARY = "summary"


# The steps asked per sport (B33), in card order.
SPORT_STEPS = (OnboardingStep.GOALS, OnboardingStep.LEVEL, OnboardingStep.SCHEDULE)
_CARD_OF = {
    OnboardingStep.ABOUT_YOU: OnboardingCard.ABOUT_YOU,
    OnboardingStep.GOALS: OnboardingCard.GOALS,
    OnboardingStep.LEVEL: OnboardingCard.LEVEL,
    OnboardingStep.SCHEDULE: OnboardingCard.SCHEDULE,
}
_STEP_OF = {card: step for step, card in _CARD_OF.items()}


@dataclass(frozen=True)
class Place:
    """A card in the athlete's flow; a sport's step names its sport."""

    card: OnboardingCard
    sport_id: str | None = None


def flow_of(sports: Sequence[str], *, sport_count: int) -> tuple[Place, ...]:
    """Every card in order for an athlete with `sports`, of `sport_count` there are: the sport,
    About you, each sport's three steps in the order picked, "Add another sport?" while a sport
    is left, and the summary."""
    places = [Place(OnboardingCard.SPORT), Place(OnboardingCard.ABOUT_YOU)]
    places += [Place(_CARD_OF[step], sport) for sport in sports for step in SPORT_STEPS]
    if len(sports) < sport_count:
        places.append(Place(OnboardingCard.MORE))
    places.append(Place(OnboardingCard.SUMMARY))
    return tuple(places)


class AlreadyPickedError(Exception):
    """Raised when "Add another sport?" is sent a sport the athlete already has."""


class EmptyAnswerError(Exception):
    """Raised when Send carries no text; Skip is how a step is left empty."""


class InvalidSummaryError(Exception):
    """Raised when a summary field is out of its bounds; `problems` names every one."""

    def __init__(self, problems: Mapping[str, FieldProblem]) -> None:
        super().__init__(f"invalid summary fields {sorted(problems)}")
        self.problems = dict(problems)


@dataclass(frozen=True)
class SportSummary:
    """One sport's section of the summary; `skipped` when all three of its steps were."""

    sport_id: str
    section: SportSection
    skipped: bool


@dataclass(frozen=True)
class OnboardingSummary:
    """What the summary shows to edit (B35): the About you fields, whether that step was
    skipped, a section per sport, and the timezone to correct (B36)."""

    about_you: AboutYou
    about_you_skipped: bool
    timezone: str
    sports: tuple[SportSummary, ...] = ()


def _sport_of(step: OnboardingStep, sport_id: str | None) -> str | None:
    """The `sport_id` a step's answer is stored under: none for About you, which is per
    athlete."""
    return None if step is OnboardingStep.ABOUT_YOU else sport_id


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
    since the summary shows it to correct. Picking the first sport again, after Back, keeps
    every sport added since and their answers (#256)."""
    registry.get(sport_id)
    if (await answers.list_sports(user_id))[:1] == [sport_id]:
        return
    zone = raw_timezone.strip()
    await answers.start(user_id, sport_id, zone if zone in timezone_names() else _FALLBACK_TIMEZONE)


async def add_sport(
    user_id: UUID, sport_id: str, *, registry: SportRegistry, answers: OnboardingAnswersPort
) -> None:
    """Adds a sport after the first one's steps (#256); its own three steps follow, and the
    answers so far are kept. An unknown sport raises `UnknownSportError`, one already picked
    `AlreadyPickedError`, and neither stores anything."""
    registry.get(sport_id)
    if sport_id in await answers.list_sports(user_id):
        raise AlreadyPickedError(sport_id)
    await answers.add_sport(user_id, sport_id)


async def current_card(
    user_id: UUID, *, answers: OnboardingAnswersPort, registry: SportRegistry
) -> Place:
    """The first card with no answer yet, sent or skipped: About you, then each sport's steps in
    the order picked; then "Add another sport?" while a sport is left, else the summary."""
    sports = await answers.list_sports(user_id)
    if not sports:
        return Place(OnboardingCard.SPORT)
    stored = {(answer.step, answer.sport_id) for answer in await answers.list_answers(user_id)}
    # The flow ends with the summary, which has no step, so there is always a first match.
    return next(
        place
        for place in flow_of(sports, sport_count=len(registry.all()))[1:]
        if (step := _STEP_OF.get(place.card)) is None or (step, place.sport_id) not in stored
    )


async def answer_of(
    user_id: UUID,
    step: OnboardingStep,
    *,
    answers: OnboardingAnswersPort,
    sport_id: str | None = None,
) -> OnboardingAnswer | None:
    """The step's stored answer, for `sport_id` on a sport's step, to prefill its card after
    Back."""
    stored = _sport_of(step, sport_id)
    return next(
        (a for a in await answers.list_answers(user_id) if a.step is step and a.sport_id == stored),
        None,
    )


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
    """Stores the answer as `pending`, under `sport_id` on a sport's step, and starts its read in
    the background, never awaiting it (ADR-0028 invariant 2). The text is trimmed and cut to the
    column's length."""
    # Postgres text cannot hold NUL; only a hand-crafted request sends one.
    cleaned = text.replace("\x00", "").strip()[:MAX_ANSWER_LENGTH].strip()
    if not cleaned:
        raise EmptyAnswerError
    stored = _sport_of(step, sport_id)
    revision = await answers.store_answer(user_id, step, stored, cleaned)
    answer = OnboardingAnswer(
        step=step, sport_id=stored, text=cleaned, revision=revision, status=AnswerStatus.PENDING
    )
    reads.start(
        (user_id, step, stored),
        read_answer(
            user_id, answer, answers=answers, reader=reader, sport_id=sport_id, timezone=timezone
        ),
    )


async def skip_step(
    user_id: UUID,
    step: OnboardingStep,
    *,
    answers: OnboardingAnswersPort,
    sport_id: str | None = None,
) -> None:
    """Marks the step skipped, under `sport_id` on a sport's step; it saves nothing and starts
    no read (B36)."""
    await answers.skip_answer(user_id, step, _sport_of(step, sport_id))


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
                # A sport's step is read with its own sport; About you with the first one.
                sport_id=answer.sport_id or sports[0],
                timezone=timezone,
            )


def _sport_summary(sport_id: str, stored: Sequence[OnboardingAnswer]) -> SportSummary:
    of_sport = {answer.step: answer for answer in stored if answer.sport_id == sport_id}

    def read(step: OnboardingStep) -> Mapping[str, object] | None:
        answer = of_sport.get(step)
        return answer.parsed if answer is not None else None

    return SportSummary(
        sport_id=sport_id,
        section=sport_from_read(
            sport_id,
            goals=read(OnboardingStep.GOALS),
            level=read(OnboardingStep.LEVEL),
            schedule=read(OnboardingStep.SCHEDULE),
        ),
        skipped=all(
            step in of_sport and of_sport[step].status is AnswerStatus.SKIPPED
            for step in SPORT_STEPS
        ),
    )


async def summary_of(
    user_id: UUID, *, answers: OnboardingAnswersPort, timezone: str
) -> OnboardingSummary:
    stored = await answers.list_answers(user_id)
    about_you = next((a for a in stored if a.step is OnboardingStep.ABOUT_YOU), None)
    return OnboardingSummary(
        about_you=about_you_from_read(about_you.parsed or {})
        if about_you is not None
        else AboutYou(),
        about_you_skipped=about_you is not None and about_you.status is AnswerStatus.SKIPPED,
        timezone=timezone,
        sports=tuple(
            _sport_summary(sport_id, stored) for sport_id in await answers.list_sports(user_id)
        ),
    )


async def confirm_summary(
    user_id: UUID,
    form: Mapping[str, str],
    *,
    sport_forms: Mapping[str, SportForm] | None = None,
    answers: OnboardingAnswersPort,
    clock: ClockPort,
) -> None:
    """Validates the edited summary and, in one transaction, writes the profile, each sport's
    profile and goals, the sports, the timezone and `onboarded_at`, and deletes the answers
    (B35, B36). Only the athlete's own sports are read from `sport_forms`, and a sport's
    problems are named `<sport_id>.<field>`. The age becomes a birth year in the athlete's own
    year. Any bad field raises `InvalidSummaryError` and writes nothing."""
    zone = form.get("timezone", "").strip()
    parsed = parse_about_you(form)
    problems: dict[str, FieldProblem] = dict(parsed) if isinstance(parsed, dict) else {}
    if zone not in timezone_names():
        problems["timezone"] = FieldProblem.UNKNOWN
    sports: Sequence[str] = await answers.list_sports(user_id)
    sections: dict[str, SportSection] = {}
    for sport_id in sports:
        typed = (sport_forms or {}).get(sport_id, SportForm())
        section = parse_sport_section(
            sport_id,
            typed.fields,
            days=typed.days,
            goal_texts=typed.goal_texts,
            goal_dates=typed.goal_dates,
        )
        if isinstance(section, dict):
            problems.update({f"{sport_id}.{field}": p for field, p in section.items()})
        else:
            sections[sport_id] = section
    if problems or isinstance(parsed, dict):
        raise InvalidSummaryError(problems)
    now = clock.now()
    sport_profiles: dict[str, SportProfile] = {
        sport_id: section.profile for sport_id, section in sections.items()
    }
    await answers.confirm(
        user_id,
        profile=profile_from(parsed, today=now.astimezone(ZoneInfo(zone)).date()),
        sport_ids=list(sports),
        sport_profiles=sport_profiles,
        goals=[goal for section in sections.values() for goal in section.goals],
        timezone=zone,
        when=now,
    )
