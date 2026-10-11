from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.typesafe import TypeSafeModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected, Evaluator, EvaluatorContext

from ai_trainer.llm.decision_gateway import OPENROUTER_SYSTEM_ONE_BASE_URL
from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals import jev_check
from ai_trainer.llm.evals.baseline import Baseline, save_baseline
from ai_trainer.llm.evals.cli import _build_parser, execute, execute_decision
from ai_trainer.llm.evals.jev_check import JevCheck
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate
from ai_trainer.settings import Settings
from tests.unit.test_eval_decisions import ScriptedJev

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


def _unauthorized(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise ModelHTTPError(
        status_code=401, model_name="under-test/model", body={"message": "No auth credentials"}
    )


async def test_execute_returns_one_and_writes_no_baseline_when_cases_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    args = _build_parser().parse_args(["run", "sample"])

    exit_code = await execute(
        args,
        settings=_settings(),
        registry=_registry(),
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        model=FunctionModel(_unauthorized),
        model_id="under-test/model",
        judge_model=FunctionModel(_reply_response),
        judge_model_id="judge/model",
        custom_evaluator_types=[_MentionsNameScore],
    )

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "1 of 1 cases failed" in stderr
    assert "no baseline written" in stderr
    assert "401" in stderr
    assert not (tmp_path / "baselines" / "sample.json").exists()


@dataclass
class _CrashingEvaluator(Evaluator[EvalCaseInputs, BaseModel]):
    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> float:
        raise RuntimeError("boom")


async def test_execute_returns_one_when_an_evaluator_crashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = Case(
        name="greet_alex",
        inputs=EvalCaseInputs(prompt="hi", deps={"name": "Alex", "sport": "climbing"}),
        expected_output=_Output(reply="hi Alex"),
    )
    path = tmp_path / "datasets" / "sample.yaml"
    path.parent.mkdir(parents=True)
    Dataset[EvalCaseInputs, _Output, object](
        name="sample", cases=[case], evaluators=[_CrashingEvaluator()]
    ).to_file(path)
    args = _build_parser().parse_args(["run", "sample"])

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
        custom_evaluator_types=[_CrashingEvaluator],
    )

    assert exit_code == 1
    assert "boom" in capsys.readouterr().err


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

    # The template run also configures Jev for any `JevCheck`, built but never called.
    configured = jev_check._configured
    assert configured is not None
    assert isinstance(configured.model, TypeSafeModel)
    assert configured.model.model_name == "typesafe/jev-1.13"


@pytest.mark.parametrize("blank", ["", "   "])
def test_main_returns_one_with_a_clear_message_when_the_api_key_is_blank(
    blank: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blank key used to crash in `OpenRouterProvider` with a traceback, before any case ran
    (found by #109's skeptic review). It now stops before the provider is built."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", blank)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(cli_module, "_default_registry", _registry)

    exit_code = cli_module.main(["run", "sample", "--model", "under-test/model"])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is blank" in stderr
    assert ".env" in stderr


def test_the_model_under_test_pins_the_templates_upstream_provider() -> None:
    settings = _settings().model_copy(update={"router_provider": "z-ai"})
    template = cli_module._default_registry().get("router", 2)

    pinned = cli_module._model_under_test(
        template, settings, model_override=None, provider=OpenRouterProvider(api_key="k")
    )

    assert pinned.model_name == settings.router_model
    assert (pinned.settings or {}).get("openrouter_provider") == {
        "order": ["z-ai"],
        "allow_fallbacks": True,
    }


def test_a_model_override_drops_the_upstream_pin() -> None:
    """The pinned upstream may not serve another model, so `--model` runs unpinned."""
    settings = _settings().model_copy(update={"router_provider": "z-ai"})
    template = cli_module._default_registry().get("router", 2)

    other = cli_module._model_under_test(
        template, settings, model_override="openai/gpt-5", provider=OpenRouterProvider(api_key="k")
    )

    assert other.model_name == "openai/gpt-5"
    assert "openrouter_provider" not in (other.settings or {})


def test_default_registry_registers_the_router_template() -> None:
    template = cli_module._default_registry().get("router", 2)

    assert template.model_settings_key == "router_model"


def test_router_evals_use_the_router_evaluators() -> None:
    from ai_trainer.llm.router import IntentKindsMatch, LogSessionSportMatch

    assert set(cli_module._evaluators_for("router")) == {IntentKindsMatch, LogSessionSportMatch}
    assert cli_module._evaluators_for("sample") == ()


def test_default_registry_registers_the_chitchat_template() -> None:
    template = cli_module._default_registry().get("chitchat", 1)

    assert template.model_settings_key == "chitchat_model"
    assert template.output_type is str


def test_chitchat_evals_add_jev_checks_to_their_built_in_judges() -> None:
    """#124: chitchat's binary rubric items are also asked of Jev, next to `LLMJudge`."""
    assert cli_module._evaluators_for("chitchat") == (JevCheck,)


def test_parser_takes_a_repeat_count_defaulting_to_one() -> None:
    parser = _build_parser()

    assert parser.parse_args(["run", "sample"]).repeat == 1
    assert parser.parse_args(["run", "sample", "--repeat", "3"]).repeat == 3


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_parser_rejects_a_repeat_below_one(value: str) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["run", "sample", "--repeat", value])


async def test_execute_runs_every_case_repeat_times(tmp_path: Path) -> None:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    args = _build_parser().parse_args(["run", "sample", "--repeat", "3"])

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
    report = (tmp_path / "reports" / "sample.json").read_text()
    assert report.count('"name": "greet_alex') == 3


# Decision sets (ADR-0020, #120): the same CLI, dispatched on the set's folder.

_DECISION_FIXTURES = Path(__file__).parent.parent / "fixtures" / "decisions"


def _write_decision_dataset(datasets_root: Path) -> None:
    datasets_root.mkdir(parents=True, exist_ok=True)
    (datasets_root / "sample.yaml").write_text(
        "name: sample\n"
        "cases:\n"
        "- name: shoulder twinge\n"
        "  inputs:\n"
        "    prompt: Bouldered two hours, left shoulder twinged.\n"
        "  expected_output:\n"
        "    mentions_pain: true\n"
    )


async def test_execute_decision_returns_zero_and_writes_the_baseline_with_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_decision_dataset(tmp_path / "datasets")
    args = _build_parser().parse_args(["run", "sample"])

    exit_code = await execute_decision(
        args,
        loader=DecisionSetLoader(root=_DECISION_FIXTURES),
        model=ScriptedJev(iter([0.82])),
        model_id="typesafe/jev-1.13",
        thresholds={"mentions_pain": 0.3},
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert exit_code == 0
    saved = Baseline.model_validate_json((tmp_path / "baselines" / "sample.json").read_text())
    assert saved.decision_thresholds == {"mentions_pain": 0.3}


async def test_execute_decision_prints_changes_and_fails_on_a_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_decision_dataset(tmp_path / "datasets")
    save_baseline(
        tmp_path / "baselines" / "sample.json",
        Baseline(
            template_id="sample",
            version=1,
            model="typesafe/jev-1.13",
            assertions=1.0,
            scores={},
            decision_thresholds={"mentions_pain": 0.5},
        ),
    )
    args = _build_parser().parse_args(["run", "sample"])

    exit_code = await execute_decision(
        args,
        loader=DecisionSetLoader(root=_DECISION_FIXTURES),
        model=ScriptedJev(iter([0.1])),
        model_id="typesafe/jev-1.13",
        thresholds={"mentions_pain": 0.3},
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CHANGED: threshold 'mentions_pain' changed from 0.5 to 0.3" in captured.out
    assert "REGRESSION: assertions dropped" in captured.err


async def test_execute_decision_returns_one_when_a_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_decision_dataset(tmp_path / "datasets")
    args = _build_parser().parse_args(["run", "sample"])

    exit_code = await execute_decision(
        args,
        loader=DecisionSetLoader(root=_DECISION_FIXTURES),
        model=ScriptedJev(iter([]), error=RuntimeError("401 from OpenRouter")),
        model_id="typesafe/jev-1.13",
        thresholds={"mentions_pain": 0.3},
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert exit_code == 1
    assert "1 of 1 cases failed" in capsys.readouterr().err


def _decision_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str = "test-key") -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(cli_module, "_DECISIONS_ROOT", _DECISION_FIXTURES)
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path / "evals")


def test_main_sends_a_decision_id_to_the_decision_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`sample` is no template in the real registry, so reaching the missing dataset proves the
    decision path ran; no model is ever called (`ALLOW_MODEL_REQUESTS` stays False)."""
    _decision_env(monkeypatch, tmp_path)

    with pytest.raises(FileNotFoundError, match=r"sample\.yaml"):
        cli_module.main(["run", "sample"])


def test_main_rejects_an_id_that_names_both_a_template_and_a_decision_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _decision_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_module, "_default_registry", _registry)

    assert cli_module.main(["run", "sample"]) == 1
    assert "both a prompt template and a decision set" in capsys.readouterr().err


def test_main_stops_on_a_blank_key_for_a_decision_set_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _decision_env(monkeypatch, tmp_path, key=" ")

    assert cli_module.main(["run", "sample"]) == 1
    assert "OPENROUTER_API_KEY is blank" in capsys.readouterr().err


def test_template_ids_still_take_the_template_path() -> None:
    assert not cli_module._is_decision_set("router")
    assert not cli_module._is_decision_set("chitchat")
    assert cli_module._is_decision_set("safety_screen")


def test_decision_thresholds_come_from_settings() -> None:
    settings = _settings().model_copy(update={"safety_screen_pain_threshold": 0.25})

    assert cli_module._decision_thresholds("safety_screen", settings) == {"pain": 0.25}
    assert cli_module._decision_thresholds("sample", settings) == {}


def test_the_decision_model_is_jev_on_openrouter_systemone_unless_overridden() -> None:
    settings = _settings()

    default = cli_module._decision_model(settings, model_override=None, api_key="k")
    other = cli_module._decision_model(settings, model_override="typesafe/jev-1.14", api_key="k")

    assert isinstance(default, TypeSafeModel)
    assert default.model_name == "typesafe/jev-1.13"
    assert default.base_url.startswith(OPENROUTER_SYSTEM_ONE_BASE_URL)
    assert other.model_name == "typesafe/jev-1.14"
