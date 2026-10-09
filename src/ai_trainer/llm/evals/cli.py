"""`ai-trainer-evals` — runs a prompt template's eval dataset against a model (ADR-0009).

    uv run ai-trainer-evals run <template-id> [--version N] [--model MODEL_ID] [--repeat N]

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
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_evals.evaluators import Evaluator

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.chitchat import chitchat_template
from ai_trainer.llm.evals.runner import (
    EvalCaseInputs,
    JudgeModelMatchesModelUnderTestError,
    run_eval,
)
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.router import IntentKindsMatch, LogSessionSportMatch, router_template
from ai_trainer.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EVALS_ROOT = _REPO_ROOT / "evals"
_PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"


def _default_registry() -> PromptRegistry:
    """M1 adds a `registry.register(...)` call here as each real template ships (ADR-0009)."""
    registry = PromptRegistry(root=_PROMPTS_ROOT)
    registry.register(router_template(default_sport_registry()))
    registry.register(chitchat_template())
    return registry


_EVALUATORS: dict[str, tuple[type[Evaluator[EvalCaseInputs, BaseModel, object]], ...]] = {
    "router": (IntentKindsMatch, LogSessionSportMatch),
}


def _evaluators_for(
    template_id: str,
) -> tuple[type[Evaluator[EvalCaseInputs, BaseModel, object]], ...]:
    """The custom `Evaluator` types a template's dataset references by name."""
    return _EVALUATORS.get(template_id, ())


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
    except JudgeModelMatchesModelUnderTestError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if result.regressions:
        for regression in result.regressions:
            print(f"REGRESSION: {regression}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings()
    registry = _default_registry()

    template = registry.get(args.template_id, args.version)
    model_id = args.model or getattr(settings, template.model_settings_key)
    judge_model_id = settings.eval_judge_model

    provider = OpenRouterProvider(api_key=settings.openrouter_api_key.get_secret_value())
    model = OpenRouterModel(model_id, provider=provider)
    judge_model = OpenRouterModel(
        judge_model_id, provider=provider, settings=ModelSettings(temperature=0)
    )

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


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover -- only runs via direct script execution
