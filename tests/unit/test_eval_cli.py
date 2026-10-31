from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.models.typesafe import TypeSafeModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected, Evaluator, EvaluatorContext

from ai_trainer.domain.llm_calls import ProviderPreferences
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.llm.chitchat import chitchat_template
from ai_trainer.llm.decision_gateway import OPENROUTER_SYSTEM_ONE_BASE_URL
from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals import jev_check
from ai_trainer.llm.evals.baseline import Baseline, save_baseline
from ai_trainer.llm.evals.cli import (
    _build_parser,
    execute,
    execute_decision,
    execute_router_fast,
)
from ai_trainer.llm.evals.jev_check import JevCheck
from ai_trainer.llm.evals.runner import EvalCaseInputs, EvalReportSummary
from ai_trainer.llm.onboarding import AboutYouMatch, GoalsMatch, LevelMatch, ScheduleMatch
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate
from ai_trainer.llm.router import ACTIVE_ROUTER_VERSION
from ai_trainer.llm.router_fast import ROUTER_FAST_VERSION, RouterFastThresholds
from ai_trainer.llm.safety_screen import SAFETY_SCREEN_VERSION
from ai_trainer.settings import Settings
from tests.unit.test_eval_decisions import ScriptedJev
from tests.unit.test_eval_router_fast import ScriptedJev as FastJev
from tests.unit.test_eval_router_fast import _case as _router_case
from tests.unit.test_eval_router_fast import _write as _write_router_cases

_FAST_THRESHOLDS = RouterFastThresholds(
    multi_request=0.2, intent=0.9, sport=0.9, plausible_floor=0.25
)
_FAST_HI = _router_case("greeting", "hi!", [{"kind": "chitchat"}])
_FAST_RIDE = _router_case("ride", "Rode 45 km", [{"kind": "log_session", "sport": "cycling"}])
_FAST_ANSWERS: dict[str, tuple[float, dict[str, tuple[str, float]]]] = {
    "hi!": (0.02, {"intent": ("chitchat", 0.97), "sport": ("gym", 0.4)}),
    "Rode 45 km": (0.03, {"intent": ("log_session", 0.96), "sport": ("cycling", 0.95)}),
}

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
    assert args.version is None
    assert args.model is None
    assert args.promote is False

    args = parser.parse_args(["run", "sample", "--version", "2", "--model", "openai/gpt-5"])
    assert args.version == 2
    assert args.model == "openai/gpt-5"


async def test_execute_returns_zero_on_a_clean_run(tmp_path: Path) -> None:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    parser = _build_parser()
    args = parser.parse_args(["run", "sample"])

    exit_code = await execute(
        args,
        version=1,
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
        version=1,
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
        version=1,
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
        version=1,
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
        version=1,
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
        cli_module.main(["run", "sample", "--version", "1", "--model", "under-test/model"])

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

    exit_code = cli_module.main(["run", "sample", "--version", "1", "--model", "under-test/model"])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is blank" in stderr
    assert ".env" in stderr


def test_the_model_under_test_routes_by_the_templates_provider_preferences() -> None:
    """The eval sends the same `provider` block the app sends (ADR-0026 step 4)."""
    settings = _settings().model_copy(
        update={"router_provider_preferences": ProviderPreferences(sort="latency")}
    )
    template = cli_module._default_registry().get("router", 2)

    pinned = cli_module._model_under_test(
        template, settings, model_override=None, provider=OpenRouterProvider(api_key="k")
    )

    assert pinned.model_name == settings.router_model
    assert (pinned.settings or {}).get("openrouter_provider") == {
        "sort": "latency",
        "allow_fallbacks": True,
    }


def test_a_model_override_drops_the_provider_preferences() -> None:
    """An `only` list or quantization may not fit another model, so `--model` runs without
    the template's preferences."""
    settings = _settings().model_copy(
        update={"router_provider_preferences": ProviderPreferences(only=["modal"])}
    )
    template = cli_module._default_registry().get("router", 2)

    other = cli_module._model_under_test(
        template, settings, model_override="openai/gpt-5", provider=OpenRouterProvider(api_key="k")
    )

    assert other.model_name == "openai/gpt-5"
    assert "openrouter_provider" not in (other.settings or {})


@pytest.mark.parametrize(
    ("template_id", "version", "field"),
    [("router", 2, "router_reasoning_effort"), ("climbing.extract", 1, "extract_reasoning_effort")],
)
def test_the_model_under_test_runs_at_the_templates_reasoning_effort(
    template_id: str, version: int, field: str
) -> None:
    """Evals measure what the app sends (ADR-0026 step 3), override or not."""
    settings = _settings().model_copy(update={field: "low"})
    template = cli_module._default_registry().get(template_id, version)

    for override in (None, "openai/gpt-5"):
        model = cli_module._model_under_test(
            template, settings, model_override=override, provider=OpenRouterProvider(api_key="k")
        )

        assert (model.settings or {}).get("openrouter_reasoning") == {"effort": "low"}


def test_the_model_under_test_sends_no_reasoning_effort_unless_set() -> None:
    template = cli_module._default_registry().get("router", 2)

    model = cli_module._model_under_test(
        template, _settings(), model_override=None, provider=OpenRouterProvider(api_key="k")
    )

    assert "openrouter_reasoning" not in (model.settings or {})


def test_default_registry_registers_the_router_template() -> None:
    template = cli_module._default_registry().get("router", 2)

    assert template.model_settings_key == "router_model"


def test_router_evals_use_the_router_evaluators() -> None:
    from ai_trainer.llm.router import IntentKindsMatch, LogSessionSportMatch

    assert set(cli_module._evaluators_for("router")) == {IntentKindsMatch, LogSessionSportMatch}
    assert cli_module._evaluators_for("sample") == ()


def test_default_registry_registers_the_chitchat_template() -> None:
    template = cli_module._default_registry().get("chitchat", 2)

    assert template.model_settings_key == "chitchat_model"
    assert template.output_type is str


def test_chitchat_evals_add_jev_checks_to_their_built_in_judges() -> None:
    """#124: chitchat's binary rubric items are also asked of Jev, next to `LLMJudge`."""
    assert cli_module._evaluators_for("chitchat") == (JevCheck,)


def test_extraction_evals_grade_the_date_and_the_activities_in_code() -> None:
    from ai_trainer.llm.extract import ActivitiesMatch, DateMatch, QuestionMatch

    # A template that may ask is graded on whether it asked, too (tickets #164 and #206).
    for template_id in ("climbing.extract", "gym.extract", "cycling.extract"):
        assert cli_module._evaluators_for(template_id) == (
            DateMatch,
            ActivitiesMatch,
            QuestionMatch,
        )


def test_default_registry_registers_climbing_extract_on_the_extract_model() -> None:
    template = cli_module._default_registry().get("climbing.extract", 1)

    assert template.model_settings_key == "extract_model"
    assert template.provider_settings_key == "extract_provider_preferences"


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
        version=1,
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
    args = _build_parser().parse_args(["run", "sample", "--promote"])

    exit_code = await execute_decision(
        args,
        version=1,
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
        version=1,
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
        version=1,
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
        cli_module.main(["run", "sample", "--version", "1"])


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

    assert cli_module.main(["run", "sample", "--version", "1"]) == 1
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


def _judge(model_id: str) -> OpenRouterModel:
    return cli_module._judge_model(model_id, provider=OpenRouterProvider(api_key="test-key"))


@pytest.mark.parametrize("model_id", ["openai/o3", "openai/gpt-5", "deepseek/deepseek-r1"])
def test_a_reasoning_judge_is_built_without_a_temperature(model_id: str) -> None:
    assert "temperature" not in (_judge(model_id).settings or {})


@pytest.mark.parametrize("model_id", ["openai/gpt-4o", "z-ai/glm-5.3"])
def test_a_non_reasoning_judge_keeps_temperature_zero(model_id: str) -> None:
    assert (_judge(model_id).settings or {}).get("temperature") == 0


def test_a_judge_with_no_reasoning_information_keeps_temperature_zero() -> None:
    judge = _judge("judge/model")

    assert judge.profile.get("openai_supports_reasoning") is None
    assert (judge.settings or {}).get("temperature") == 0


def test_main_builds_a_reasoning_judge_without_a_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, OpenRouterModel] = {}

    async def fake_execute(*args: object, judge_model: OpenRouterModel, **kwargs: object) -> int:
        captured["judge"] = judge_model
        return 0

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai/o3")
    monkeypatch.setattr(cli_module, "_default_registry", _registry)
    monkeypatch.setattr(cli_module, "execute", fake_execute)

    assert cli_module.main(["run", "sample", "--version", "1", "--model", "under-test/model"]) == 0
    assert "temperature" not in (captured["judge"].settings or {})


# `router_fast` runs on the router's cases and is graded against the router (#122).


async def test_execute_router_fast_returns_zero_and_writes_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_router_cases(tmp_path, [_FAST_HI, _FAST_RIDE])

    exit_code = await execute_router_fast(
        _build_parser().parse_args(["run", "router_fast", "--promote"]),
        version=1,
        loader=DecisionSetLoader(),
        model=FastJev(_FAST_ANSWERS),
        model_id="typesafe/jev-1.13",
        thresholds=_FAST_THRESHOLDS,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert exit_code == 0
    saved = Baseline.model_validate_json((tmp_path / "baselines" / "router_fast.json").read_text())
    assert saved.scores == {"coverage": 1.0, "accuracy": 1.0}


async def test_execute_router_fast_returns_one_when_less_accurate_than_the_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_router_cases(tmp_path, [_FAST_HI, _FAST_RIDE])
    wrong = {
        **_FAST_ANSWERS,
        "hi!": (0.02, {"intent": ("request_plan", 0.97), "sport": ("gym", 0.4)}),
    }

    exit_code = await execute_router_fast(
        _build_parser().parse_args(["run", "router_fast"]),
        version=1,
        loader=DecisionSetLoader(),
        model=FastJev(wrong),
        model_id="typesafe/jev-1.13",
        thresholds=_FAST_THRESHOLDS,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert exit_code == 1
    assert "below the router's 0.900" in capsys.readouterr().err


async def test_execute_router_fast_fails_on_a_regression_and_prints_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_router_cases(tmp_path, [_FAST_HI, _FAST_RIDE])
    save_baseline(
        tmp_path / "baselines" / "router_fast.json",
        Baseline(
            template_id="router_fast",
            version=1,
            model="typesafe/jev-1.13",
            assertions=1.0,
            scores={"coverage": 1.0, "accuracy": 1.0},
            decision_thresholds={"multi_request": 0.2, "intent": 0.99, "sport": 0.9},
        ),
    )
    declines_the_ride = {
        **_FAST_ANSWERS,
        "Rode 45 km": (0.5, {"intent": ("log_session", 0.96), "sport": ("cycling", 0.95)}),
    }

    exit_code = await execute_router_fast(
        _build_parser().parse_args(["run", "router_fast"]),
        version=1,
        loader=DecisionSetLoader(),
        model=FastJev(declines_the_ride),
        model_id="typesafe/jev-1.13",
        thresholds=_FAST_THRESHOLDS,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CHANGED: threshold 'intent' changed from 0.99 to 0.9" in captured.out
    assert "REGRESSION: score 'coverage' dropped" in captured.err


def test_main_sends_router_fast_to_its_own_runner_with_settings_thresholds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reaching the missing router dataset under the patched evals root proves the router_fast
    path ran; no model is ever called (`ALLOW_MODEL_REQUESTS` stays False)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path / "evals")
    save_baseline(
        tmp_path / "evals" / "baselines" / "router.json",
        Baseline(template_id="router", version=2, model="m", assertions=0.9, scores={}),
    )

    with pytest.raises(FileNotFoundError, match=r"router\.yaml"):
        cli_module.main(["run", "router_fast"])


def test_router_fast_thresholds_come_from_settings() -> None:
    settings = _settings().model_copy(
        update={
            "router_fast_multi_request_threshold": 0.1,
            "router_fast_intent_threshold": 0.85,
            "router_fast_sport_threshold": 0.95,
            "router_fast_plausible_sport_floor": 0.2,
        }
    )

    assert cli_module._router_fast_thresholds(settings) == RouterFastThresholds(
        multi_request=0.1, intent=0.85, sport=0.95, plausible_floor=0.2
    )


# The version run by default is the app's, read from where the app reads it (#208).


def _template_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    """Real wiring up to `execute`, which only records the version it was handed."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path / "evals")
    versions: list[int] = []

    async def fake_execute(*args: object, version: int, **kwargs: object) -> int:
        versions.append(version)
        return 0

    async def fake_execute_decision(*args: object, version: int, **kwargs: object) -> int:
        versions.append(version)
        return 0

    monkeypatch.setattr(cli_module, "execute", fake_execute)
    monkeypatch.setattr(cli_module, "execute_decision", fake_execute_decision)
    monkeypatch.setattr(cli_module, "execute_router_fast", fake_execute_decision)
    return versions


def test_a_run_without_version_runs_an_extraction_at_its_plugins_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    versions = _template_env(monkeypatch, tmp_path)
    climbing = default_sport_registry().get("climbing").extraction
    assert climbing is not None

    assert cli_module.main(["run", "climbing.extract"]) == 0
    assert versions == [climbing.version] == [3]


@pytest.mark.parametrize(
    ("item_id", "active"),
    [
        ("router", ACTIVE_ROUTER_VERSION),
        ("router_fast", ROUTER_FAST_VERSION),
        ("chitchat", chitchat_template().version),
        ("safety_screen", SAFETY_SCREEN_VERSION),
    ],
)
def test_a_run_without_version_runs_the_version_the_app_loads(
    item_id: str, active: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    versions = _template_env(monkeypatch, tmp_path)

    assert cli_module.main(["run", item_id]) == 0
    assert versions == [active]


def test_an_explicit_version_still_runs_that_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    versions = _template_env(monkeypatch, tmp_path)

    assert cli_module.main(["run", "climbing.extract", "--version", "2"]) == 0
    assert versions == [2]


def test_onboarding_about_you_runs_at_its_version_graded_on_converted_values() -> None:
    """ADR-0028: the template evals measure is the one the app runs, graded in code."""
    assert cli_module._active_versions(default_sport_registry())["onboarding.about_you"] == 1
    assert cli_module._evaluators_for("onboarding.about_you") == (AboutYouMatch,)
    assert cli_module._default_registry().versions("onboarding.about_you") == [1]


@pytest.mark.parametrize(
    ("template_id", "evaluator"),
    [
        ("onboarding.goals", GoalsMatch),
        ("onboarding.level", LevelMatch),
        ("onboarding.schedule", ScheduleMatch),
    ],
)
def test_the_sport_steps_run_at_their_version_graded_in_code(
    template_id: str, evaluator: type[object]
) -> None:
    """ADR-0028, ticket #255: each sport step's template is registered and graded in code."""
    assert cli_module._active_versions(default_sport_registry())[template_id] == 1
    assert cli_module._evaluators_for(template_id) == (evaluator,)
    assert cli_module._default_registry().versions(template_id) == [1]


def test_the_default_follows_a_plugins_active_version() -> None:
    """Bumping a plugin's `ExtractionSpec.version` moves the eval with no edit here."""
    sports = default_sport_registry()
    gym = sports.get("gym")
    assert gym.extraction is not None
    bumped = SportRegistry(
        [
            gym.model_copy(update={"extraction": gym.extraction.model_copy(update={"version": 7})})
            if plugin.id == "gym"
            else plugin
            for plugin in sports.all()
        ]
    )

    assert cli_module._active_versions(bumped)["gym.extract"] == 7


@pytest.mark.parametrize(
    ("argv", "line"),
    [
        (
            ["run", "climbing.extract", "--version", "9"],
            "No version 9: climbing.extract has versions 1, 2, 3",
        ),
        (["run", "safety_screen", "--version", "9"], "No version 9: safety_screen has versions 1"),
        (
            ["run", "no_such_template"],
            "No prompt template or decision set named 'no_such_template'",
        ),
    ],
)
def test_an_unregistered_version_or_id_exits_with_one_line_before_any_model(
    argv: list[str],
    line: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versions = _template_env(monkeypatch, tmp_path)

    def no_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("no model may be built for an unregistered version")

    monkeypatch.setattr(cli_module, "OpenRouterProvider", no_model)
    monkeypatch.setattr(cli_module, "TypeSafeProvider", no_model)

    assert cli_module.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.err == line + "\n"
    assert versions == []
    assert not (tmp_path / "evals").exists()


def test_an_id_with_no_version_the_app_runs_asks_for_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fixture template is registered, but the app runs no version of it."""
    versions = _template_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_module, "_default_registry", _registry)

    assert cli_module.main(["run", "sample"]) == 1
    assert capsys.readouterr().err == "The app runs no version of sample; pass --version 1\n"
    assert versions == []


async def test_execute_prints_the_id_version_and_model_before_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    printed_first: list[str] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        printed_first.append(capsys.readouterr().out)
        return _reply_response(messages, info)

    await execute(
        _build_parser().parse_args(["run", "sample"]),
        version=1,
        settings=_settings(),
        registry=_registry(),
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        model=FunctionModel(reply),
        model_id="under-test/model",
        judge_model=FunctionModel(_reply_response),
        judge_model_id="judge/model",
        custom_evaluator_types=[_MentionsNameScore],
    )

    assert printed_first == ["sample v1 (under-test/model)\n"]


async def test_execute_decision_and_router_fast_print_their_header_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_decision_dataset(tmp_path / "datasets")
    _write_router_cases(tmp_path, [_FAST_HI])

    await execute_decision(
        _build_parser().parse_args(["run", "sample"]),
        version=1,
        loader=DecisionSetLoader(root=_DECISION_FIXTURES),
        model=ScriptedJev(iter([0.82])),
        model_id="typesafe/jev-1.13",
        thresholds={"mentions_pain": 0.3},
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )
    assert capsys.readouterr().out.startswith("sample v1 (typesafe/jev-1.13)\n")

    await execute_router_fast(
        _build_parser().parse_args(["run", "router_fast"]),
        version=1,
        loader=DecisionSetLoader(),
        model=FastJev(_FAST_ANSWERS),
        model_id="typesafe/jev-1.13",
        thresholds=_FAST_THRESHOLDS,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )
    assert capsys.readouterr().out.startswith("router_fast v1 (typesafe/jev-1.13)\n")


# A run writes the baseline only with `--promote` (#208).


def _committed_sample_baseline(tmp_path: Path) -> Path:
    path = tmp_path / "baselines" / "sample.json"
    save_baseline(
        path,
        Baseline(
            template_id="sample",
            version=1,
            model="under-test/model",
            assertions=1.0,
            scores={"_MentionsNameScore": 1.0},
        ),
    )
    return path


async def _execute_regressing(tmp_path: Path, argv: list[str]) -> int:
    def no_name(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('{"reply": "hello there"}')])

    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    return await execute(
        _build_parser().parse_args(argv),
        version=1,
        settings=_settings(),
        registry=_registry(),
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        model=FunctionModel(no_name),
        model_id="under-test/model",
        judge_model=FunctionModel(_reply_response),
        judge_model_id="judge/model",
        custom_evaluator_types=[_MentionsNameScore],
    )


async def test_a_regressing_run_without_promote_leaves_the_baseline_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _committed_sample_baseline(tmp_path)
    before = path.read_bytes()

    assert await _execute_regressing(tmp_path, ["run", "sample"]) == 1

    assert path.read_bytes() == before
    captured = capsys.readouterr()
    assert "REGRESSION: assertions dropped" in captured.err
    assert "baseline not written (pass --promote to write it)" in captured.out


async def test_a_promoted_regression_writes_the_baseline_and_prints_the_drop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _committed_sample_baseline(tmp_path)

    assert await _execute_regressing(tmp_path, ["run", "sample", "--promote"]) == 1

    saved = Baseline.model_validate_json(path.read_text())
    assert (saved.assertions, saved.scores) == (0.0, {"_MentionsNameScore": 0.0})
    captured = capsys.readouterr()
    assert "REGRESSION: assertions dropped from 1.000 to 0.000" in captured.err
    assert f"PROMOTED: {path}" in captured.out


async def test_a_decision_run_without_promote_writes_no_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    _write_decision_dataset(tmp_path / "datasets")

    exit_code = await execute_decision(
        _build_parser().parse_args(["run", "sample"]),
        version=1,
        loader=DecisionSetLoader(root=_DECISION_FIXTURES),
        model=ScriptedJev(iter([0.82])),
        model_id="typesafe/jev-1.13",
        thresholds={"mentions_pain": 0.3},
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert exit_code == 0
    assert not (tmp_path / "baselines").exists()
    assert "baseline not written (pass --promote to write it)" in capsys.readouterr().out


# An older version still reads a dataset holding cases it can't return (#208).

_GYM_REFERENCE_CASE = "multi-activity: 5x5 squat then 3 sets of pull-ups (AC)"


async def test_gym_extract_v2_grades_question_cases_as_failed_question_matches(
    tmp_path: Path,
) -> None:
    """v2 can only draft, so each `reading:` case, which expects a question, fails
    `QuestionMatch`, while every draft case is graded by all three evaluators as before. The
    model returns one draft case's own expected draft, which passes that case outright."""
    from ai_trainer.domain.sports.gym.extraction import GymExtraction, GymQuestion

    datasets_root = cli_module._EVALS_ROOT / "datasets"
    dataset = Dataset[EvalCaseInputs, GymExtraction | GymQuestion, object].from_file(
        datasets_root / "gym.extract.yaml",
        custom_evaluator_types=cli_module._evaluators_for("gym.extract"),
    )
    [reference] = [case for case in dataset.cases if case.name == _GYM_REFERENCE_CASE]
    assert isinstance(reference.expected_output, GymExtraction)
    draft = reference.expected_output.model_dump(mode="json")
    offered: set[tuple[str, ...]] = set()

    def drafts_only(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.add(tuple(tool.name for tool in info.output_tools))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, draft)])

    exit_code = await execute(
        _build_parser().parse_args(["run", "gym.extract", "--version", "2"]),
        version=2,
        settings=_settings(),
        registry=cli_module._default_registry(),
        datasets_root=datasets_root,
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        model=FunctionModel(drafts_only),
        model_id="under-test/model",
        judge_model=FunctionModel(drafts_only),
        judge_model_id="judge/model",
        custom_evaluator_types=cli_module._evaluators_for("gym.extract"),
        expected_output_types=cli_module._expected_output_types("gym.extract"),
    )

    assert exit_code == 0
    assert offered == {("final_result",)}
    report = EvalReportSummary.model_validate_json(
        (tmp_path / "reports" / "gym.extract.json").read_text()
    )
    assert report.name == "gym.extract v2 (under-test/model)"
    asks = {
        str(case.name) for case in dataset.cases if isinstance(case.expected_output, GymQuestion)
    }
    assert len(asks) == 6
    for case in report.cases:
        if case.name in asks:
            assert case.assertions == {"QuestionMatch": False}, case.name
        else:
            assert set(case.assertions) == {"DateMatch", "ActivitiesMatch", "QuestionMatch"}
            assert case.assertions["QuestionMatch"] is True, case.name
    [graded] = [case for case in report.cases if case.name == _GYM_REFERENCE_CASE]
    assert all(graded.assertions.values())


def test_only_an_extraction_that_may_ask_reads_its_dataset_with_its_question() -> None:
    from ai_trainer.domain.sports.gym.extraction import GymExtraction, GymQuestion

    assert cli_module._expected_output_types("gym.extract") == (GymExtraction, GymQuestion)
    assert cli_module._expected_output_types("router") is None
