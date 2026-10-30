"""The AI onboarding's routes (PRD 0008 F6, F20, F21, ADR-0028, tickets #254-#256), wired from
one sub-router per step (ticket #331): the sport card (`start`), the About you chat card
(`about_you`), each sport's goals, level and schedule chat cards under `/onboarding/<sport_id>/`
(`sport_steps`), all with Skip and Back, "Add another sport?" (`more`), and the summary that
catches up on reads over SSE, with its confirm (`summary`). No request here awaits a model call
except the summary stream (invariant 2), and `user_id` comes only from the authenticated
session (ADR-0005)."""

from fastapi import APIRouter
from starlette.templating import Jinja2Templates

from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingAnswersPort, OnboardingReaderPort
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.web.ai_onboarding.about_you import build_about_you_router
from ai_trainer.web.ai_onboarding.context import OnboardingContext
from ai_trainer.web.ai_onboarding.more import build_more_router
from ai_trainer.web.ai_onboarding.sport_steps import build_sport_steps_router
from ai_trainer.web.ai_onboarding.start import build_start_router
from ai_trainer.web.ai_onboarding.summary import build_summary_router


def build_ai_onboarding_router(
    templates: Jinja2Templates,
    *,
    registry: SportRegistry,
    answers: OnboardingAnswersPort,
    reads: BackgroundReads,
    reader: OnboardingReaderPort,
    clock: ClockPort,
) -> APIRouter:
    ctx = OnboardingContext(
        templates=templates,
        registry=registry,
        answers=answers,
        reads=reads,
        reader=reader,
        clock=clock,
    )
    router = APIRouter()
    # In this order, so the routes and the OpenAPI document stay as they were.
    router.include_router(build_start_router(ctx))
    router.include_router(build_about_you_router(ctx))
    router.include_router(build_sport_steps_router(ctx))
    router.include_router(build_more_router(ctx))
    router.include_router(build_summary_router(ctx))
    return router
