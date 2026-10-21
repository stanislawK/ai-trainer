"""`ai-trainer-evals` — runs a prompt template's or a decision set's eval dataset against a
model (ADR-0009, ADR-0020).

    uv run ai-trainer-evals run <template-or-decision-id> [--version N] [--model ID] [--repeat N]
        [--promote]

Without `--version`, the run is of the version the app runs. A run writes its report and diffs
against the committed baseline; only `--promote` writes the baseline (#208).

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

from ai_trainer.domain.llm_calls import ProviderPreferences, ReasoningEffort
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.llm.chitchat import CHITCHAT_TEMPLATE_ID, chitchat_template
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
from ai_trainer.llm.extract import ActivitiesMatch, DateMatch, QuestionMatch
from ai_trainer.llm.gateway import provider_config, reasoning_at
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate, TemplateOutput
from ai_trainer.llm.router import (
    ACTIVE_ROUTER_VERSION,
    ROUTER_TEMPLATE_ID,
    IntentKindsMatch,
    LogSessionSportMatch,
)
from ai_trainer.llm.router_fast import ROUTER_FAST_ID, ROUTER_FAST_VERSION, RouterFastThresholds
from ai_trainer.llm.safety_screen import SAFETY_SCREEN_ID, SAFETY_SCREEN_VERSION
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
    # Every sport's extraction template is graded the same way, in code (ADR-0006, ADR-0014),
    # and one that may ask on whether it asked (ticket #164).
    **{
        plugin.extraction.template_id: (DateMatch, ActivitiesMatch, QuestionMatch)
        if plugin.extraction.question is not None
        else (DateMatch, ActivitiesMatch)
        for plugin in default_sport_registry().all()
        if plugin.extraction is not None
    },
}


def _evaluators_for(
    template_id: str,
) -> tuple[type[Evaluator[EvalCaseInputs, BaseModel, object]], ...]:
    """The custom `Evaluator` types a template's dataset references by name."""
    return _EVALUATORS.get(template_id, ())


# Every output an extraction that may ask can return, at any version: a version from before
# its question still reads the dataset's question cases, and fails them (#208).
_EXPECTED_OUTPUTS: dict[str, tuple[type[BaseModel], ...]] = {
    plugin.extraction.template_id: (plugin.extraction.output_type, plugin.extraction.question.type)
    for plugin in default_sport_registry().all()
    if plugin.extraction is not None and plugin.extraction.question is not None
}


def _expected_output_types(template_id: str) -> tuple[type[BaseModel], ...] | None:
    """The types a template's dataset is read with, when they're more than its own outputs."""
    return _EXPECTED_OUTPUTS.get(template_id)


def _active_versions(sports: SportRegistry) -> dict[str, int]:
    """The version of each template and decision set the app runs, read from where the app
    reads it, so a run without `--version` measures what ships (#208)."""
    return {
        ROUTER_TEMPLATE_ID: ACTIVE_ROUTER_VERSION,
        CHITCHAT_TEMPLATE_ID: chitchat_template().version,
        **{
            plugin.extraction.template_id: plugin.extraction.version
            for plugin in sports.all()
            if plugin.extraction is not None
        },
        ROUTER_FAST_ID: ROUTER_FAST_VERSION,
        SAFETY_SCREEN_ID: SAFETY_SCREEN_VERSION,
    }


def _version_to_run(item_id: str, requested: int | None, known: list[int]) -> int | None:
    """`requested`, or the app's version; `None`, with one line printed, when it isn't one of
    `known`, before any key is read or model built."""
    version = (
        requested
        if requested is not None
        else _active_versions(default_sport_registry()).get(item_id)
    )
    if version is None:
        print(
            f"The app runs no version of {item_id}; pass --version "
            + " or ".join(str(number) for number in known),
            file=sys.stderr,
        )
        return None
    if version not in known:
        print(
            f"No version {version}: {item_id} has versions "
            + ", ".join(str(number) for number in known),
            file=sys.stderr,
        )
        return None
    return version


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
        plausible_floor=settings.router_fast_plausible_sport_floor,
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
    run_parser.add_argument(
        "--version", type=int, default=None, help="Defaults to the version the app runs"
    )
    run_parser.add_argument("--model", default=None, help="Overrides the model under test")
    run_parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=1,
        help="Runs every case N times; the baseline averages over all runs",
    )
    run_parser.add_argument(
        "--promote",
        action="store_true",
        help="Writes the run's figures to the committed baseline, even on a regression",
    )

    return parser


async def execute(
    args: argparse.Namespace,
    *,
    version: int,
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
    expected_output_types: tuple[type[BaseModel], ...] | None = None,
) -> int:
    """The testable core: `model`/`judge_model` are already-resolved `Model` instances, so a
    test injects a scripted `FunctionModel` here instead of a real `OpenRouterModel`."""
    _print_header(args.template_id, version, model_id)
    try:
        result = await run_eval(
            registry=registry,
            template_id=args.template_id,
            version=version,
            model=model,
            model_id=model_id,
            judge_model=judge_model,
            judge_model_id=judge_model_id,
            datasets_root=datasets_root,
            baselines_root=baselines_root,
            reports_root=reports_root,
            custom_evaluator_types=custom_evaluator_types,
            repeat=args.repeat,
            promote=args.promote,
            expected_output_types=expected_output_types,
        )
    except (JudgeModelMatchesModelUnderTestError, CaseFailedError, EvaluatorFailedError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return _report_outcome(
        args, baselines_root=baselines_root, changes=[], regressions=result.regressions
    )


def _print_header(item_id: str, version: int, model_id: str) -> None:
    """What the run measures, before its first model call (#208)."""
    print(f"{item_id} v{version} ({model_id})", flush=True)


def _report_outcome(
    args: argparse.Namespace,
    *,
    baselines_root: Path,
    changes: Sequence[str],
    regressions: Sequence[str],
) -> int:
    """Prints what changed, each regression, and whether the baseline was written; a promoted
    regression is written too, for the owner to approve at the `/tune-prompt` gate (#208)."""
    for change in changes:
        print(f"CHANGED: {change}")
    for regression in regressions:
        print(f"REGRESSION: {regression}", file=sys.stderr)
    if args.promote:
        print(f"PROMOTED: {baselines_root / f'{args.template_id}.json'}")
    else:
        print("baseline not written (pass --promote to write it)")
    return 1 if regressions else 0


async def execute_decision(
    args: argparse.Namespace,
    *,
    version: int,
    loader: DecisionSetLoader,
    model: Model,
    model_id: str,
    thresholds: dict[str, float],
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
) -> int:
    """The decision-set counterpart of `execute`; a test injects a fake `DecisionModel`."""
    _print_header(args.template_id, version, model_id)
    try:
        result = await run_decision_eval(
            loader=loader,
            decision_id=args.template_id,
            version=version,
            model=model,
            model_id=model_id,
            thresholds=thresholds,
            datasets_root=datasets_root,
            baselines_root=baselines_root,
            reports_root=reports_root,
            repeat=args.repeat,
            promote=args.promote,
        )
    except (CaseFailedError, EvaluatorFailedError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return _report_outcome(
        args,
        baselines_root=baselines_root,
        changes=result.changes,
        regressions=result.regressions,
    )


async def execute_router_fast(
    args: argparse.Namespace,
    *,
    version: int,
    loader: DecisionSetLoader,
    model: Model,
    model_id: str,
    thresholds: RouterFastThresholds,
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
) -> int:
    """`router_fast`'s counterpart of `execute_decision`; a test injects a fake `DecisionModel`."""
    _print_header(args.template_id, version, model_id)
    try:
        result = await run_router_fast_eval(
            loader=loader,
            version=version,
            model=model,
            model_id=model_id,
            thresholds=thresholds,
            datasets_root=datasets_root,
            baselines_root=baselines_root,
            reports_root=reports_root,
            repeat=args.repeat,
            promote=args.promote,
        )
    except (CaseFailedError, EvaluatorFailedError, FastPathAccuracyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return _report_outcome(
        args,
        baselines_root=baselines_root,
        changes=result.changes,
        regressions=result.regressions,
    )


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
    """The template's model from `Settings`, with its provider preferences and at its reasoning
    effort, as the app sends it. A `--model` override drops the preferences, since an `only`
    list or quantization may not fit that model, but keeps the effort."""
    model_settings = OpenRouterModelSettings()
    effort: ReasoningEffort | None = (
        getattr(settings, template.reasoning_effort_settings_key)
        if template.reasoning_effort_settings_key is not None
        else None
    )
    if effort is not None:
        model_settings["openrouter_reasoning"] = reasoning_at(effort)
    if model_override is not None:
        return OpenRouterModel(model_override, provider=provider, settings=model_settings or None)
    model_id: str = getattr(settings, template.model_settings_key)
    preferences: ProviderPreferences | None = (
        getattr(settings, template.provider_settings_key)
        if template.provider_settings_key is not None
        else None
    )
    if preferences is not None:
        model_settings["openrouter_provider"] = provider_config(preferences)
    return OpenRouterModel(model_id, provider=provider, settings=model_settings or None)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings()
    registry = _default_registry()
    loader = DecisionSetLoader(root=_DECISIONS_ROOT)
    template_versions = registry.versions(args.template_id)

    if _is_decision_set(args.template_id):
        if template_versions:
            print(
                f"{args.template_id!r} is both a prompt template and a decision set; rename one.",
                file=sys.stderr,
            )
            return 1
        version = _version_to_run(args.template_id, args.version, loader.versions(args.template_id))
        if version is None:
            return 1
        return _main_decision(args, settings, version=version, loader=loader)

    if not template_versions:
        print(f"No prompt template or decision set named {args.template_id!r}", file=sys.stderr)
        return 1
    version = _version_to_run(args.template_id, args.version, template_versions)
    if version is None:
        return 1
    template = registry.get(args.template_id, version)
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
            version=version,
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
            expected_output_types=_expected_output_types(args.template_id),
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


def _main_decision(
    args: argparse.Namespace, settings: Settings, *, version: int, loader: DecisionSetLoader
) -> int:
    api_key = _api_key(settings)
    if api_key is None:
        return 1
    model = _decision_model(settings, model_override=args.model, api_key=api_key)
    if args.template_id == ROUTER_FAST_ID:
        return asyncio.run(
            execute_router_fast(
                args,
                version=version,
                loader=loader,
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
            version=version,
            loader=loader,
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
