"""Fails on prompt text written as a Python string literal outside `tests/` (ADR-0008).

Run with `uv run python scripts/check_prompt_literals.py`. Prompt bodies belong in
`src/ai_trainer/llm/prompts/<id>/v<N>.<locale>.md`, loaded through `PromptRegistry` and passed
around as variables — never as a literal string (or f-string) handed straight to
`Agent(instructions=...)`, `Agent(system_prompt=...)`, an agent's `.run(instructions=...)`,
`LlmGatewayPort.run(prompt=...)`, or `TemplateStr(...)`/`TemplateStr(source=...)`.
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_PROMPT_KEYWORDS = {"instructions", "system_prompt", "prompt"}
_TEMPLATE_STR_CALLEES = {"TemplateStr"}


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

    return violations


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
