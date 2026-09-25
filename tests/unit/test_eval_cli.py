from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected, Evaluator, EvaluatorContext

from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.baseline import Baseline, save_baseline
from ai_trainer.llm.evals.cli import _build_parser, execute
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate
from ai_trainer.settings import Settings

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "prompts"


class _Deps(BaseModel):
    name: str
    sport: str


class _Output(BaseModel):
    reply: str


_SAMPLE_TEMPLATE = PromptTemplate(
    id="sample",
    version=1,
    locale="en",
    deps_type=_Deps,
    output_type=_Output,
    model_settings_key="sample_template_model",
)


@dataclass
class _MentionsNameScore(Evaluator[EvalCaseInputs, BaseModel]):
    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> float:
        assert isinstance(ctx.output, _Output)
        return 1.0 if str(ctx.inputs.deps.get("name", "")) in ctx.output.reply else 0.0


class _SettingsWithSampleModel(Settings):
    sample_template_model: str = "under-test/model"


def _settings() -> _SettingsWithSampleModel:
    return _SettingsWithSampleModel(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="judge/model",
    )


def _registry() -> PromptRegistry:
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(_SAMPLE_TEMPLATE)
    return registry


def _write_sample_dataset(path: Path) -> None:
    case = Case(
        name="greet_alex",
        inputs=EvalCaseInputs(prompt="hi", deps={"name": "Alex", "sport": "climbing"}),
        expected_output=_Output(reply="hi Alex"),
    )
    dataset = Dataset[EvalCaseInputs, _Output, object](
        name="sample",
        cases=[case],
        evaluators=[EqualsExpected(), _MentionsNameScore()],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_file(path)


def _reply_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart('{"reply": "hi Alex"}')])


def test_parser_parses_the_required_template_id_and_optional_overrides() -> None:
    parser = _build_parser()

    args = parser.parse_args(["run", "sample"])
    assert args.template_id == "sample"
    assert args.version == 1
    assert args.model is None

    args = parser.parse_args(["run", "sample", "--version", "2", "--model", "openai/gpt-5"])
    assert args.version == 2
    assert args.model == "openai/gpt-5"


async def test_execute_returns_zero_on_a_clean_run(tmp_path: Path) -> None:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    parser = _build_parser()
    args = parser.parse_args(["run", "sample"])

    exit_code = await execute(
        args,
        settings=_settings(),
        registry=_registry(),
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        model=FunctionModel(_reply_response),
        model_id="under-test/model",
        judge_model=FunctionModel(_reply_response),
        judge_model_id="judge/model",
        custom_evaluator_types=[_MentionsNameScore],
    )

    assert exit_code == 0


async def test_execute_returns_one_when_the_judge_model_matches_the_model_under_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    parser = _build_parser()
    args = parser.parse_args(["run", "sample"])

    exit_code = await execute(
        args,
        settings=_settings(),
        registry=_registry(),
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        model=FunctionModel(_reply_response),
        model_id="same/model",
        judge_model=FunctionModel(_reply_response),
        judge_model_id="same/model",
        custom_evaluator_types=[_MentionsNameScore],
    )

    assert exit_code == 1
    assert "same/model" in capsys.readouterr().err


async def test_execute_returns_one_on_a_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_baseline(
        tmp_path / "baselines" / "sample.json",
        Baseline(
            template_id="sample",
            version=1,
            model="under-test/model",
            assertions=1.0,
            scores={"_MentionsNameScore": 1.0},
        ),
    )

    def _no_name_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('{"reply": "hello there"}')])

    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    parser = _build_parser()
    args = parser.parse_args(["run", "sample"])

    exit_code = await execute(
        args,
        settings=_settings(),
        registry=_registry(),
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        model=FunctionModel(_no_name_response),
        model_id="under-test/model",
        judge_model=FunctionModel(_reply_response),
        judge_model_id="judge/model",
        custom_evaluator_types=[_MentionsNameScore],
    )

    assert exit_code == 1
    assert "REGRESSION" in capsys.readouterr().err


def test_default_registry_points_at_the_real_prompts_root() -> None:
    registry = cli_module._default_registry()

    assert isinstance(registry, PromptRegistry)
    assert "training companion" in registry.load_persona()


def test_main_wires_real_settings_and_openrouter_before_reaching_the_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main()` is the thin, real-wiring entry point (`Settings()`, a real
    `OpenRouterProvider`/`OpenRouterModel`, `asyncio.run`) that `execute`'s own tests bypass
    by injecting a scripted model directly. This proves `main` builds all of that and reaches
    `execute`, then lets the expected missing-dataset `FileNotFoundError` (no dataset ships
    with this ticket — M1 adds the first ones) prove it, without ever calling a real model
    (ADR-0013 invariant 1: `conftest.py` sets `ALLOW_MODEL_REQUESTS = False` for every test)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(cli_module, "_default_registry", _registry)

    with pytest.raises(FileNotFoundError):
        cli_module.main(["run", "sample", "--model", "under-test/model"])
