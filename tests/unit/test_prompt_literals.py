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
