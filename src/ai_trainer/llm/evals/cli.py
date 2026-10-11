"""`ai-trainer-evals` — runs a prompt template's or a decision set's eval dataset against a
model (ADR-0009, ADR-0020).

    uv run ai-trainer-evals run <template-or-decision-id> [--version N] [--model ID] [--repeat N]

An ID with a folder under `llm/decisions/` is a decision set: it runs on Jev, graded at its
thresholds from `Settings`, with no judge model. `router_fast` runs on the router's cases and
is graded against the router's baseline (`llm/evals/router_fast.py`).

Real OpenRouter calls cost money; this is never invoked by `pytest` or by push/pull_request
CI (ADR-0013), only on demand — locally or through the `workflow_dispatch`-only GitHub
Actions job.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import ModelSettings
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.models.typesafe import TypeSafeModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.providers.typesafe import TypeSafeProvider
from pydantic_evals.evaluators import Evaluator

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.decision_gateway import OPENROUTER_SYSTEM_ONE_BASE_URL
from ai_trainer.llm.decisions.loader import DECISIONS_ROOT, DecisionSetLoader
from ai_trainer.llm.evals.decisions import run_decision_eval
from ai_trainer.llm.evals.jev_check import JevCheck, configure_jev_check
from ai_trainer.llm.evals.router_fast import FastPathAccuracyError, run_router_fast_eval
from ai_trainer.llm.evals.runner import (
    CaseFailedError,
    EvalCaseInputs,
    EvaluatorFailedError,
    JudgeModelMatchesModelUnderTestError,
    run_eval,
)
from ai_trainer.llm.gateway import pinned_upstream
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import (
    PromptTemplate,
    TemplateOutput,
    UnknownPromptTemplateError,
)
from ai_trainer.llm.router import IntentKindsMatch, LogSessionSportMatch
from ai_trainer.llm.router_fast import ROUTER_FAST_ID, RouterFastThresholds
from ai_trainer.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EVALS_ROOT = _REPO_ROOT / "evals"
_PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"
_DECISIONS_ROOT = DECISIONS_ROOT

# Each decision set's probability questions and the `Settings` field holding the threshold
# its label is graded at (ADR-0020); the baseline records the values.
_DECISION_THRESHOLDS: dict[str, dict[str, str]] = {
    "safety_screen": {"pain": "safety_screen_pain_threshold"},
}


def _default_registry() -> PromptRegistry:
    """The app's own templates (`build_prompt_registry`), so evals run what the app runs."""
    return build_prompt_registry(_PROMPTS_ROOT, default_sport_registry())


_EVALUATORS: dict[str, tuple[type[Evaluator[EvalCaseInputs, BaseModel, object]], ...]] = {
    "router": (IntentKindsMatch, LogSessionSportMatch),
    "chitchat": (JevCheck,),
}


def _evaluators_for(
    template_id: str,
) -> tuple[type[Evaluator[EvalCaseInputs, BaseModel, object]], ...]:
    """The custom `Evaluator` types a template's dataset references by name."""
    return _EVALUATORS.get(template_id, ())


def _is_decision_set(item_id: str) -> bool:
    return (_DECISIONS_ROOT / item_id).is_dir()


def _decision_thresholds(decision_id: str, settings: Settings) -> dict[str, float]:
    return {
        question: float(getattr(settings, field))
        for question, field in _DECISION_THRESHOLDS.get(decision_id, {}).items()
    }


def _router_fast_thresholds(settings: Settings) -> RouterFastThresholds:
    return RouterFastThresholds(
        multi_request=settings.router_fast_multi_request_threshold,
        intent=settings.router_fast_intent_threshold,
        sport=settings.router_fast_sport_threshold,
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-trainer-evals")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a template's eval dataset")
    run_parser.add_argument("template_id")
    run_parser.add_argument("--version", type=int, default=1)
    run_parser.add_argument("--model", default=None, help="Overrides the model under test")
    run_parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=1,
        help="Runs every case N times; the baseline averages over all runs",
    )

    return parser


async def execute(
    args: argparse.Namespace,
    *,
    settings: Settings,
    registry: PromptRegistry,
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
    model: Model,
    model_id: str,
    judge_model: Model,
    judge_model_id: str,
    custom_evaluator_types: Sequence[type[Evaluator[EvalCaseInputs, BaseModel, object]]] = (),
) -> int:
    """The testable core: `model`/`judge_model` are already-resolved `Model` instances, so a
    test injects a scripted `FunctionModel` here instead of a real `OpenRouterModel`."""
    try:
        result = await run_eval(
            registry=registry,
            template_id=args.template_id,
            version=args.version,
            model=model,
            model_id=model_id,
            judge_model=judge_model,
            judge_model_id=judge_model_id,
            datasets_root=datasets_root,
            baselines_root=baselines_root,
            reports_root=reports_root,
            custom_evaluator_types=custom_evaluator_types,
            repeat=args.repeat,
        )
    except (JudgeModelMatchesModelUnderTestError, CaseFailedError, EvaluatorFailedError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if result.regressions:
        for regression in result.regressions:
            print(f"REGRESSION: {regression}", file=sys.stderr)
        return 1
    return 0


async def execute_decision(
    args: argparse.Namespace,
    *,
    loader: DecisionSetLoader,
    model: Model,
    model_id: str,
    thresholds: dict[str, float],
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
) -> int:
    """The decision-set counterpart of `execute`; a test injects a fake `DecisionModel`."""
    try:
        result = await run_decision_eval(
            loader=loader,
            decision_id=args.template_id,
            version=args.version,
            model=model,
            model_id=model_id,
            thresholds=thresholds,
            datasets_root=datasets_root,
            baselines_root=baselines_root,
            reports_root=reports_root,
            repeat=args.repeat,
        )
    except (CaseFailedError, EvaluatorFailedError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for change in result.changes:
        print(f"CHANGED: {change}")
    for regression in result.regressions:
        print(f"REGRESSION: {regression}", file=sys.stderr)
    return 1 if result.regressions else 0


async def execute_router_fast(
    args: argparse.Namespace,
    *,
    loader: DecisionSetLoader,
    model: Model,
    model_id: str,
    thresholds: RouterFastThresholds,
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
) -> int:
    """`router_fast`'s counterpart of `execute_decision`; a test injects a fake `DecisionModel`."""
    try:
        result = await run_router_fast_eval(
            loader=loader,
            version=args.version,
            model=model,
            model_id=model_id,
            thresholds=thresholds,
            datasets_root=datasets_root,
            baselines_root=baselines_root,
            reports_root=reports_root,
            repeat=args.repeat,
        )
    except (CaseFailedError, EvaluatorFailedError, FastPathAccuracyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for change in result.changes:
        print(f"CHANGED: {change}")
    for regression in result.regressions:
        print(f"REGRESSION: {regression}", file=sys.stderr)
    return 1 if result.regressions else 0


def _decision_model(settings: Settings, *, model_override: str | None, api_key: str) -> Model:
    """Jev through OpenRouter's TypeSafe-compatible endpoint, as the app calls it (ADR-0020)."""
    provider = TypeSafeProvider(base_url=OPENROUTER_SYSTEM_ONE_BASE_URL, api_key=api_key)
    return TypeSafeModel(model_override or settings.decision_model, provider=provider)


def _model_under_test(
    template: PromptTemplate[BaseModel, TemplateOutput],
    settings: Settings,
    *,
    model_override: str | None,
    provider: OpenRouterProvider,
) -> OpenRouterModel:
    """The template's model from `Settings`, pinned to its upstream like the app pins it. A
    `--model` override runs unpinned: the pinned upstream may not serve that model."""
    if model_override is not None:
        return OpenRouterModel(model_override, provider=provider)
    model_id: str = getattr(settings, template.model_settings_key)
    upstream: str | None = (
        getattr(settings, template.provider_settings_key)
        if template.provider_settings_key is not None
        else None
    )
    model_settings = (
        OpenRouterModelSettings(openrouter_provider=pinned_upstream(upstream))
        if upstream is not None
        else None
    )
    return OpenRouterModel(model_id, provider=provider, settings=model_settings)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings()
    registry = _default_registry()

    if _is_decision_set(args.template_id):
        try:
            registry.get(args.template_id, args.version)
        except UnknownPromptTemplateError:
            return _main_decision(args, settings)
        print(
            f"{args.template_id!r} is both a prompt template and a decision set; rename one.",
            file=sys.stderr,
        )
        return 1

    template = registry.get(args.template_id, args.version)
    judge_model_id = settings.eval_judge_model

    api_key = _api_key(settings)
    if api_key is None:
        return 1
    # Built, never called, unless the dataset holds a `JevCheck` (ADR-0009 amendment).
    configure_jev_check(_decision_model(settings, model_override=None, api_key=api_key))
    provider = OpenRouterProvider(api_key=api_key)
    model = _model_under_test(template, settings, model_override=args.model, provider=provider)
    model_id = model.model_name
    judge_model = _judge_model(judge_model_id, provider=provider)

    return asyncio.run(
        execute(
            args,
            settings=settings,
            registry=registry,
            datasets_root=_EVALS_ROOT / "datasets",
            baselines_root=_EVALS_ROOT / "baselines",
            reports_root=_EVALS_ROOT / "reports",
            model=model,
            model_id=model_id,
            judge_model=judge_model,
            judge_model_id=judge_model_id,
            custom_evaluator_types=_evaluators_for(args.template_id),
        )
    )


def _judge_model(model_id: str, *, provider: OpenRouterProvider) -> OpenRouterModel:
    """The judge at `temperature=0` (ADR-0009), unless its profile says it reasons: Pydantic AI
    drops sampling parameters from a reasoning model and warns on every request. The profile
    decides, not a list of IDs; an ID whose profile says nothing keeps `temperature=0`."""
    judge = OpenRouterModel(model_id, provider=provider)
    profile = judge.profile
    if profile.get("openai_supports_reasoning") or profile.get("thinking_always_enabled"):
        return judge
    return OpenRouterModel(model_id, provider=provider, settings=ModelSettings(temperature=0))


def _api_key(settings: Settings) -> str | None:
    api_key = settings.openrouter_api_key.get_secret_value()
    if not api_key.strip():
        # `OpenRouterProvider` would raise a bare `UserError` traceback here (found at #109).
        print(
            "OPENROUTER_API_KEY is blank: set it in .env to your OpenRouter key, then re-run.",
            file=sys.stderr,
        )
        return None
    return api_key


def _main_decision(args: argparse.Namespace, settings: Settings) -> int:
    api_key = _api_key(settings)
    if api_key is None:
        return 1
    model = _decision_model(settings, model_override=args.model, api_key=api_key)
    if args.template_id == ROUTER_FAST_ID:
        return asyncio.run(
            execute_router_fast(
                args,
                loader=DecisionSetLoader(root=_DECISIONS_ROOT),
                model=model,
                model_id=model.model_name,
                thresholds=_router_fast_thresholds(settings),
                datasets_root=_EVALS_ROOT / "datasets",
                baselines_root=_EVALS_ROOT / "baselines",
                reports_root=_EVALS_ROOT / "reports",
            )
        )
    return asyncio.run(
        execute_decision(
            args,
            loader=DecisionSetLoader(root=_DECISIONS_ROOT),
            model=model,
            model_id=model.model_name,
            thresholds=_decision_thresholds(args.template_id, settings),
            datasets_root=_EVALS_ROOT / "datasets",
            baselines_root=_EVALS_ROOT / "baselines",
            reports_root=_EVALS_ROOT / "reports",
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover -- only runs via direct script execution
