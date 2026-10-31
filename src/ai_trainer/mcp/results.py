"""The result envelope every MCP tool returns (ADR-0010 invariant 3).

`fastmcp.tools` and `pydantic_ai.mcp` each have their own `ToolResult`; import this one from
here and never next to either (`.claude/rules/mcp.md`)."""

from typing import Self

from pydantic import BaseModel, ConfigDict


class ToolResult[T](BaseModel):
    """A tool's typed output in `data` on success. A domain failure (bad input, an empty
    result, a spent budget) is `success=False` with an `error` and a `recovery_hint` telling
    the caller how to retry, so the model corrects itself instead of meeting an exception."""

    model_config = ConfigDict(frozen=True)

    success: bool
    data: T | None = None
    error: str | None = None
    recovery_hint: str | None = None

    @classmethod
    def ok(cls, data: T) -> Self:
        return cls(success=True, data=data)

    @classmethod
    def failed(cls, error: str, recovery_hint: str) -> Self:
        return cls(success=False, error=error, recovery_hint=recovery_hint)
