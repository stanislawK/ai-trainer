"""`media.picture_brief` (ADR-0031 *Picture generation*, #532): a text model turns an exercise's
name and public description into a brief for the image model, naming the start and end
position and forbidding printed text; with no description at all there is no brief, and no
model call."""

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import PostgresDsn, SecretStr, ValidationError
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EvaluatorContext

from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import PageLocator
from ai_trainer.domain.knowledge_search import (
    KnowledgeHit,
    KnowledgeSearchFailed,
    KnowledgeSearchPage,
)
from ai_trainer.domain.media_generation import (
    BriefPassage,
    NoBrief,
    PictureBrief,
    PictureBriefDeps,
    needs_no_model,
    picture_brief_deps,
)
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import read_smoke_set, run_smoke_set
from ai_trainer.llm.picture_brief import (
    PICTURE_BRIEF_TEMPLATE_ID,
    PICTURE_BRIEF_VERSION,
    PictureBriefMatch,
    picture_brief_template,
)
from ai_trainer.settings import Settings

_ROOT = Path(__file__).parents[2]
_PROMPTS_ROOT = _ROOT / "src" / "ai_trainer" / "llm" / "prompts"
_SMOKE_ROOT = _ROOT / "evals" / "smoke"
_BENCH_TEXT = ExerciseText(
    lang="en",
    instructions="Lie flat on a bench with your feet flat on the ground.",
    steps=("Lie flat on a bench.", "Lower the barbell to your chest."),
)


def _registry() -> Any:
    return build_prompt_registry(_PROMPTS_ROOT, default_sport_registry())


def _hit(title: str, text: str) -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id=uuid4(),
        text=text,
        heading_path=(),
        language=SourceLanguage.EN,
        source_id=uuid4(),
        title=title,
        author=None,
        evidence_tier=EvidenceTier.COACHING_BOOK,
        source_type=SourceType.BOOK,
        source_url=None,
        attribution=None,
        locator=PageLocator(page=1),
        exercise_id=None,
    )


def _brief(**changes: str) -> PictureBrief:
    fields = {
        "exercise_name": "Pull-Up",
        "start_position": "hanging from the bar with arms straight",
        "end_position": "chest at the bar with elbows bent",
        "image_prompt": (
            "One person hanging from a pull-up bar with arms straight, then with the chest at "
            "the bar and elbows bent. Plain grey background. No text, labels or arrows."
        ),
    }
    return PictureBrief.model_validate(fields | changes)


def _ctx(
    output: PictureBrief | NoBrief,
    expected: PictureBrief | NoBrief,
    *,
    name: str = "Pull-Up",
    forbids: list[str] | None = None,
) -> EvaluatorContext[EvalCaseInputs, Any]:
    deps = PictureBriefDeps(exercise_name=name, description="Hang from a bar.", passages=[])
    return EvaluatorContext(
        name="case",
        inputs=EvalCaseInputs(prompt="Write the brief.", deps=deps.model_dump()),
        metadata={"expect": {"forbids": forbids}} if forbids is not None else None,
        expected_output=expected,
        output=output,
        duration=0.0,
        _span_tree=None,  # type: ignore[arg-type]  # not read by this evaluator
        attributes={},
        metrics={},
    )


# AC: renders from a deps model with no athlete identity.
def test_template_renders_from_deps_with_no_identity() -> None:
    deps = PictureBriefDeps(
        exercise_name="Barbell Bench Press",
        description="Lie flat on a bench with your feet flat on the ground.",
        passages=[BriefPassage(title="Strength book", text="Keep the shoulder blades back.")],
    )

    rendered = _registry().render_instructions(
        PICTURE_BRIEF_TEMPLATE_ID, PICTURE_BRIEF_VERSION, deps
    )

    assert "Barbell Bench Press" in rendered
    assert "feet flat on the ground" in rendered
    assert "Strength book" in rendered
    assert "Keep the shoulder blades back." in rendered
    assert set(PictureBriefDeps.model_fields) == {"exercise_name", "description", "passages"}
    with pytest.raises(ValidationError):
        PictureBriefDeps.model_validate(
            {"exercise_name": "Pull-Up", "description": None, "passages": [], "user_id": "x"}
        )


def test_template_has_no_persona_reads_at_zero_and_its_own_model_setting() -> None:
    template = picture_brief_template()

    assert template.persona is False
    assert template.temperature == 0
    assert template.model_settings_key == "picture_brief_model"
    assert template.output_type == (PictureBrief, NoBrief)


def test_build_prompt_registry_registers_picture_brief() -> None:
    assert _registry().versions(PICTURE_BRIEF_TEMPLATE_ID) == [PICTURE_BRIEF_VERSION]


def test_picture_brief_model_defaults_to_a_stable_model() -> None:
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
    )

    assert settings.picture_brief_model == "z-ai/glm-5.3-flash"
    blank = settings.model_validate(settings.model_dump() | {"picture_brief_model": " "})
    assert blank.picture_brief_model == "z-ai/glm-5.3-flash"


# AC: each brief names start and end and forbids text and labels.
def test_body_requires_positions_and_no_text() -> None:
    body = (_PROMPTS_ROOT / PICTURE_BRIEF_TEMPLATE_ID / "v1.en.md").read_text().lower()

    assert "start position" in body
    assert "end position" in body
    assert "one figure" in body
    assert "plain background" in body
    assert "no text, letters, numbers, labels or arrows" in body


def test_picture_brief_match_grades_positions_and_no_text_rule() -> None:
    evaluator = PictureBriefMatch()

    assert all(evaluator.evaluate(_ctx(_brief(), _brief())).values())  # type: ignore[union-attr]
    no_start = evaluator.evaluate(_ctx(_brief(start_position=" "), _brief()))
    assert no_start == {**no_start, "positions_named": False}  # type: ignore[dict-item]
    no_rule = evaluator.evaluate(
        _ctx(_brief(image_prompt="A person doing a pull-up, then at the top."), _brief())
    )
    assert no_rule["no_text_rule"] is False  # type: ignore[index]


def test_picture_brief_match_needs_the_position_itself_in_the_prompt() -> None:
    # Only filler words are shared with the prompt: the position isn't restated.
    filler_only = _brief(
        start_position="hanging from the bar with arms straight",
        image_prompt="One figure with a plain background, from the side. No text or labels.",
    )

    result = PictureBriefMatch().evaluate(_ctx(filler_only, _brief()))

    assert result["positions_named"] is False  # type: ignore[index]


def test_picture_brief_match_reads_other_forms_of_a_position_word() -> None:
    reworded = _brief(
        end_position="arms extended to the bar",
        image_prompt=(
            "One person hanging from a bar with arms straight, then the right arm extending to "
            "the bar. Plain background. No text or labels."
        ),
    )

    result = PictureBriefMatch().evaluate(_ctx(reworded, _brief()))

    assert result["positions_named"] is True  # type: ignore[index]


def test_picture_brief_match_needs_the_no_text_rule_itself() -> None:
    evaluator = PictureBriefMatch()
    stated = (
        "No text, labels or arrows.",
        "Draw it without any text or labels.",
        "No printed labels in the picture.",
    )
    loose = (
        "No props; a person holding a sign with words on it.",
        "Without a bench, label the muscles with text.",
    )

    for rule in stated:
        brief = _brief(image_prompt=f"{_brief().image_prompt.split('. Plain')[0]}. {rule}")
        assert evaluator.evaluate(_ctx(brief, _brief()))["no_text_rule"] is True, rule  # type: ignore[index]
    for rule in loose:
        brief = _brief(image_prompt=f"{_brief().image_prompt.split('. Plain')[0]}. {rule}")
        assert evaluator.evaluate(_ctx(brief, _brief()))["no_text_rule"] is False, rule  # type: ignore[index]


def test_picture_brief_match_fails_a_renamed_exercise() -> None:
    result = PictureBriefMatch().evaluate(_ctx(_brief(exercise_name="Chin-Up"), _brief()))

    assert result["name_unchanged"] is False  # type: ignore[index]


def test_picture_brief_match_fails_the_wrong_kind() -> None:
    evaluator = PictureBriefMatch()

    assert evaluator.evaluate(_ctx(NoBrief(reason="none"), _brief())) == {"kind": False}
    assert evaluator.evaluate(_ctx(_brief(), NoBrief(reason="none"))) == {"kind": False}
    assert evaluator.evaluate(_ctx(NoBrief(reason="a"), NoBrief(reason="b"))) == {"kind": True}


# AC (edge): a contradicting passage is not copied.
def test_picture_brief_match_fails_a_forbidden_detail() -> None:
    copied = _brief(end_position="chest at the bar with feet up on the bench")

    result = PictureBriefMatch().evaluate(
        _ctx(copied, _brief(), forbids=["feet (up )?on the bench"])
    )

    assert result["forbids"] is False  # type: ignore[index]


# AC: no description and no passage is "no brief", with no model call.
def test_no_description_and_no_passage_is_no_brief_without_a_model() -> None:
    empty = PictureBriefDeps(exercise_name="Pull-Up", description=None, passages=[])
    blank = PictureBriefDeps(exercise_name="Pull-Up", description="  ", passages=[])

    assert isinstance(needs_no_model(empty), NoBrief)
    assert isinstance(needs_no_model(blank), NoBrief)
    assert needs_no_model(empty.model_copy(update={"description": "Hang from a bar."})) is None
    passage_only = empty.model_copy(update={"passages": [BriefPassage(title="t", text="Hang.")]})
    assert needs_no_model(passage_only) is None


# AC: passages come only from the run's own search.
def test_deps_take_passages_only_from_the_search_page() -> None:
    page = KnowledgeSearchPage(
        hits=(_hit("Book A", "First."), _hit("Book B", "Second.")), truncated=False
    )

    deps = picture_brief_deps("Barbell Bench Press", _BENCH_TEXT, page)

    assert deps.exercise_name == "Barbell Bench Press"
    assert deps.description == _BENCH_TEXT.instructions
    assert deps.passages == [
        BriefPassage(title="Book A", text="First."),
        BriefPassage(title="Book B", text="Second."),
    ]
    failed = picture_brief_deps("Pull-Up", None, KnowledgeSearchFailed(reason="no_match"))
    assert failed.passages == []
    assert failed.description is None


def test_deps_drop_a_passage_with_no_text_so_no_model_is_called() -> None:
    page = KnowledgeSearchPage(hits=(_hit("Book A", "  "),), truncated=False)

    deps = picture_brief_deps("Pull-Up", None, page)

    assert deps.passages == []
    assert isinstance(needs_no_model(deps), NoBrief)


def test_deps_fall_back_to_the_steps_when_the_instructions_are_blank() -> None:
    text = ExerciseText(lang="en", instructions=" ", steps=("Hang.", "Pull up."))

    deps = picture_brief_deps("Pull-Up", text, KnowledgeSearchFailed(reason="no_match"))

    assert deps.description == "Hang.\nPull up."


# AC: the smoke cases are graded in code and read with no judge.
def test_the_smoke_set_reads_with_no_judge_and_eight_cases() -> None:
    versions = cli_module._active_versions(default_sport_registry())

    smoke_set = read_smoke_set(
        PICTURE_BRIEF_TEMPLATE_ID, smoke_root=_SMOKE_ROOT, known_templates=versions
    )

    assert versions[PICTURE_BRIEF_TEMPLATE_ID] == PICTURE_BRIEF_VERSION
    assert smoke_set.has_judge is False
    assert len(smoke_set.cases) == 8
    assert cli_module._evaluators_for(PICTURE_BRIEF_TEMPLATE_ID) == (PictureBriefMatch,)
    for case in smoke_set.cases:
        PictureBriefDeps.model_validate(case.inputs.deps)
        assert case.metadata is not None
        assert "source" in case.metadata


def test_every_synthetic_passage_in_the_smoke_set_is_marked() -> None:
    raw = yaml.safe_load((_SMOKE_ROOT / f"{PICTURE_BRIEF_TEMPLATE_ID}.yaml").read_text())

    for case in raw["cases"]:
        if case["inputs"]["deps"]["passages"]:
            assert case["metadata"]["knowledge_source"] == "synthetic", case["name"]


def _answer_from_the_smoke_case(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    # Copies the exercise name into a brief that satisfies the grader, or says there's none.
    instructions = str(messages[0].instructions)  # type: ignore[union-attr]
    match = re.search(r"Exercise: (.+)", instructions)
    assert match is not None
    name = match.group(1).strip()
    passages = instructions.split("Passages:")[1].split("## Rules")[0]
    if "Description: none" in instructions and not passages.strip():
        [tool] = [output.name for output in info.output_tools if "NoBrief" in output.name]
        return ModelResponse(parts=[ToolCallPart(tool, {"reason": "nothing describes it"})])
    [tool] = [output.name for output in info.output_tools if "PictureBrief" in output.name]
    args = {
        "exercise_name": name,
        "start_position": "starting stance",
        "end_position": "finishing stance",
        "image_prompt": (
            "One person in the starting stance, then the finishing stance, on a plain "
            "background. No text, labels or arrows."
        ),
    }
    return ModelResponse(parts=[ToolCallPart(tool, args)])


async def test_the_smoke_set_runs_and_grades_on_a_scripted_model() -> None:
    smoke_set = read_smoke_set(PICTURE_BRIEF_TEMPLATE_ID, smoke_root=_SMOKE_ROOT)

    results = await run_smoke_set(
        smoke_set,
        registry=_registry(),
        version=PICTURE_BRIEF_VERSION,
        model=FunctionModel(_answer_from_the_smoke_case),
        model_id="scripted",
        evaluator_types=(PictureBriefMatch,),
    )

    assert [result.detail for result in results if not result.passed] == []


def test_each_reference_brief_in_the_smoke_set_passes_its_own_grader() -> None:
    raw = yaml.safe_load((_SMOKE_ROOT / f"{PICTURE_BRIEF_TEMPLATE_ID}.yaml").read_text())

    for case in raw["cases"]:
        expected = case["expected_output"]
        reference = (
            PictureBrief.model_validate(expected)
            if "image_prompt" in expected
            else NoBrief.model_validate(expected)
        )
        deps = PictureBriefDeps.model_validate(case["inputs"]["deps"])
        ctx = EvaluatorContext[EvalCaseInputs, Any](
            name=case["name"],
            inputs=EvalCaseInputs(prompt=case["inputs"]["prompt"], deps=deps.model_dump()),
            metadata=case["metadata"],
            expected_output=reference,
            output=reference,
            duration=0.0,
            _span_tree=None,  # type: ignore[arg-type]  # not read by this evaluator
            attributes={},
            metrics={},
        )
        assert all(PictureBriefMatch().evaluate(ctx).values()), case["name"]  # type: ignore[union-attr]
