from pathlib import Path

import pytest

from scripts.check_prompt_literals import find_violations, main


def test_no_prompt_literals_in_src() -> None:
    assert find_violations(Path("src")) == []


def test_detects_a_literal_instructions_keyword(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_agent.py"
    bad_file.write_text(
        "from pydantic_ai import Agent\n"
        "agent = Agent('test', instructions='You are a helpful assistant.')\n"
    )

    violations = find_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].path == bad_file
    assert violations[0].line == 2


def test_detects_a_literal_template_str_body(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_template.py"
    bad_file.write_text(
        "from pydantic_ai import TemplateStr\n"
        "template = TemplateStr('Hello {{name}}, welcome back.')\n"
    )

    violations = find_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].line == 2


def test_detects_a_literal_template_str_source_keyword(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_template_kwarg.py"
    bad_file.write_text(
        "from pydantic_ai import TemplateStr\n"
        "template = TemplateStr(source='Hello {{name}}, welcome back.')\n"
    )

    violations = find_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].line == 2


def test_detects_an_f_string_instructions_keyword(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_fstring.py"
    bad_file.write_text(
        "from pydantic_ai import Agent\n"
        "name = 'assistant'\n"
        "agent = Agent('test', instructions=f'You are a helpful {name}.')\n"
    )

    violations = find_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].line == 3


def test_detects_a_literal_prompt_keyword_on_the_gateway_port(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad_gateway_call.py"
    bad_file.write_text(
        "gateway.run(instructions=instructions, prompt='You are a helpful assistant.')\n"
    )

    violations = find_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].line == 1


def test_ignores_calls_passing_a_variable_instead_of_a_literal(tmp_path: Path) -> None:
    ok_file = tmp_path / "ok_agent.py"
    ok_file.write_text(
        "from pydantic_ai import Agent\n"
        "persona = load_persona()\n"
        "agent = Agent('test', instructions=persona)\n"
    )

    assert find_violations(tmp_path) == []


def test_ignores_a_template_str_style_call_through_a_subscript_callee(
    tmp_path: Path,
) -> None:
    # `fns[0](...)` has no Name/Attribute callee, so the TemplateStr(...) name match can't
    # apply — exercises `_callee_name`'s fallback `None` return.
    ok_file = tmp_path / "dynamic_call.py"
    ok_file.write_text("fns = [str]\nfns[0]('a literal with {{var}} in it')\n")

    assert find_violations(tmp_path) == []


def test_main_returns_zero_and_prints_nothing_for_a_clean_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_file = tmp_path / "clean.py"
    clean_file.write_text("x = 1\n")

    exit_code = main(tmp_path)

    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_main_returns_one_and_prints_each_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_file = tmp_path / "bad.py"
    bad_file.write_text(
        "from pydantic_ai import Agent\n"
        "agent = Agent('test', instructions='You are a helpful assistant.')\n"
    )

    exit_code = main(tmp_path)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert f"{bad_file}:2" in out


@pytest.mark.parametrize(
    "source",
    [
        "from pydantic import Field\nx: float = Field(ge=0, le=1, description='Is it painful?')\n",
        "from pydantic_ai import BoolCriteria\nc = BoolCriteria(true='It hurts.', false=f'{ok}')\n",
        "from pydantic_ai import Choices\nS = Choices({'gym': 'Weights in a gym.'})\n",
        "from pydantic_ai import Choices\nS = Choices(options, description='Which sport?')\n",
        "from pydantic_ai import Choice\nc = Choice('Any card starting with a 4.', value=4)\n",
        "from pydantic_ai import Choice\nc = Choice(description='Any card.', value=4)\n",
    ],
)
def test_detects_decision_question_text_as_a_literal(tmp_path: Path, source: str) -> None:
    bad_file = tmp_path / "bad_decision.py"
    bad_file.write_text(source)

    violations = find_violations(tmp_path)

    assert len(violations) >= 1
    assert {violation.line for violation in violations} == {2}


def test_ignores_decision_question_text_passed_as_variables(tmp_path: Path) -> None:
    ok_file = tmp_path / "ok_decision.py"
    ok_file.write_text(
        "from pydantic import Field\n"
        "from pydantic_ai import BoolCriteria, Choices\n"
        "x: float = Field(ge=0, le=1, description=spec.question)\n"
        "c = BoolCriteria(true=spec.true, false=spec.false)\n"
        "S = Choices(dict(spec.options), name='sport', description=spec.question)\n"
        "err = OAuthError(description='google sign-in failed')\n"
    )

    assert find_violations(tmp_path) == []


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        (
            "from pydantic import create_model\nM = create_model('M', __doc__='Decide pain.')\n",
            {2},
        ),
        (
            "from pydantic import BaseModel, ConfigDict\n"
            "class Pain(BaseModel):\n"
            "    model_config = ConfigDict(use_attribute_docstrings=True)\n"
            "    painful: float\n"
            "    '''Is the athlete in pain?'''\n",
            {5},
        ),
        (
            "from enum import Enum\n"
            "from pydantic_ai import UseEnumMemberDocstrings\n"
            "class Sport(UseEnumMemberDocstrings, str, Enum):\n"
            "    '''Which sport the note is about.'''\n"
            "    GYM = 'gym'\n"
            "    '''Weights in a gym.'''\n",
            {4, 6},
        ),
    ],
)
def test_detects_decision_text_written_as_a_docstring(
    tmp_path: Path, source: str, lines: set[int]
) -> None:
    bad_file = tmp_path / "bad_docstrings.py"
    bad_file.write_text(source)

    assert {violation.line for violation in find_violations(tmp_path)} == lines


def test_ignores_ordinary_class_and_attribute_docstrings(tmp_path: Path) -> None:
    ok_file = tmp_path / "ok_docstrings.py"
    ok_file.write_text(
        "from enum import Enum\n"
        "from pydantic import BaseModel\n"
        "class Deps(BaseModel):\n"
        "    '''The router's variables.'''\n"
        "    message: str\n"
        "    '''The athlete's message.'''\n"
        "class Kind(str, Enum):\n"
        "    '''Kinds.'''\n"
        "    A = 'a'\n"
        "    '''First.'''\n"
        "M = create_model('M', __doc__=spec.goal)\n"
    )

    assert find_violations(tmp_path) == []
