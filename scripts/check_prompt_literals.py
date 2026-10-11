"""Fails on prompt text written as a Python string literal outside `tests/` (ADR-0008).

Run with `uv run python scripts/check_prompt_literals.py`. Prompt bodies belong in
`src/ai_trainer/llm/prompts/<id>/v<N>.<locale>.md`, loaded through `PromptRegistry` and passed
around as variables — never as a literal string (or f-string) handed straight to
`Agent(instructions=...)`, `Agent(system_prompt=...)`, an agent's `.run(instructions=...)`,
`LlmGatewayPort.run(prompt=...)`, or `TemplateStr(...)`/`TemplateStr(source=...)`.

Decision question text is prompt text too (ADR-0020 invariant 1): it lives in
`src/ai_trainer/llm/decisions/<id>/v<N>.en.yaml`, so a literal in `Field(description=...)`,
`BoolCriteria(true=..., false=...)`, `Choices({...: ...}, description=...)` or
`Choice(..., description=...)` is flagged as well, and so are the docstrings Pydantic AI reads as
decision text: `create_model(__doc__=...)`, attribute docstrings in a model with
`use_attribute_docstrings=True`, and the docstrings of an enum mixing in
`UseEnumMemberDocstrings`. An ordinary class docstring is not flagged.
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_PROMPT_KEYWORDS = {"instructions", "system_prompt", "prompt"}
_TEMPLATE_STR_CALLEES = {"TemplateStr"}
# Callees whose keywords carry a decision question, an option's meaning or a yes/no's criteria.
_DECISION_TEXT_KEYWORDS = {
    "Field": {"description"},
    "Choices": {"description"},
    "Choice": {"description"},
    "BoolCriteria": {"true", "false"},
    "create_model": {"__doc__"},
}


@dataclass(frozen=True, slots=True)
class PromptLiteralViolation:
    path: Path
    line: int
    message: str


def find_violations(root: Path) -> list[PromptLiteralViolation]:
    violations: list[PromptLiteralViolation] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                violations.extend(_check_call(node, path))
            elif isinstance(node, ast.ClassDef):
                violations.extend(_docstring_violations(node, path))
    return violations


def _check_call(node: ast.Call, path: Path) -> list[PromptLiteralViolation]:
    violations: list[PromptLiteralViolation] = []

    if _callee_name(node.func) in _TEMPLATE_STR_CALLEES:
        if node.args:
            violations.extend(_literal_violations(node.args[0], path, "TemplateStr(...) body"))
        for keyword in node.keywords:
            if keyword.arg == "source" and keyword.value is not None:
                violations.extend(
                    _literal_violations(keyword.value, path, "TemplateStr(source=...)")
                )

    for keyword in node.keywords:
        if keyword.arg in _PROMPT_KEYWORDS and keyword.value is not None:
            violations.extend(_literal_violations(keyword.value, path, f"{keyword.arg}="))

    violations.extend(_decision_text_violations(node, path))
    return violations


def _decision_text_violations(node: ast.Call, path: Path) -> list[PromptLiteralViolation]:
    callee = _callee_name(node.func)
    if callee not in _DECISION_TEXT_KEYWORDS:
        return []
    violations: list[PromptLiteralViolation] = []
    for keyword in node.keywords:
        if keyword.arg in _DECISION_TEXT_KEYWORDS[callee]:
            violations.extend(_literal_violations(keyword.value, path, f"{callee}({keyword.arg}=)"))
    if callee == "Choice" and node.args:
        violations.extend(_literal_violations(node.args[0], path, "Choice(...) description"))
    if callee == "Choices" and node.args and isinstance(node.args[0], ast.Dict):
        for value in node.args[0].values:
            violations.extend(_literal_violations(value, path, "Choices({...}) option"))
    return violations


def _docstring_violations(node: ast.ClassDef, path: Path) -> list[PromptLiteralViolation]:
    """Docstrings that Pydantic AI sends as decision text rather than reading as code docs."""
    if any(_callee_name(base) == "UseEnumMemberDocstrings" for base in node.bases):
        docstrings = [
            statement.value
            for statement in node.body
            if isinstance(statement, ast.Expr) and _is_string_literal(statement.value)
        ]
    elif _uses_attribute_docstrings(node):
        docstrings = [
            statement.value
            for previous, statement in zip(node.body, node.body[1:], strict=False)
            if isinstance(previous, ast.AnnAssign | ast.Assign)
            and isinstance(statement, ast.Expr)
            and _is_string_literal(statement.value)
        ]
    else:
        return []
    return [
        PromptLiteralViolation(
            path=path,
            line=docstring.lineno,
            message=f"{path}:{docstring.lineno}: decision text as a docstring in {node.name}",
        )
        for docstring in docstrings
    ]


def _uses_attribute_docstrings(node: ast.ClassDef) -> bool:
    return any(
        keyword.arg == "use_attribute_docstrings"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for statement in node.body
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call)
        for keyword in statement.value.keywords
    )


def _literal_violations(value: ast.expr, path: Path, where: str) -> list[PromptLiteralViolation]:
    candidates: list[ast.expr] = [value]
    if isinstance(value, ast.List | ast.Tuple):
        candidates = list(value.elts)

    return [
        PromptLiteralViolation(
            path=path,
            line=candidate.lineno,
            message=f"{path}:{candidate.lineno}: prompt text as a Python string literal in {where}",
        )
        for candidate in candidates
        if _is_string_literal(candidate)
    ]


def _is_string_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    # An f-string (`ast.JoinedStr`) is still hand-written prompt text, whether or not any of
    # its pieces are interpolated.
    return isinstance(node, ast.JoinedStr)


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


DEFAULT_ROOT = Path("src")


def main(root: Path = DEFAULT_ROOT) -> int:
    violations = find_violations(root)
    for violation in violations:
        print(violation.message)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution, not import
