# Research — MCP tool design and server patterns

| | |
|---|---|
| Ticket | #369 |
| Read on | 2026-10-31 |
| Feeds | ADR-0010; #378 (the in-process server) and every later MCP tool, including M1g's `find_exercise` and `exercise_history` and M3's `search_knowledge` |

Versions read: MCP specification `2026-07-28`, FastMCP 3.4.11 and 4.1.4 (docs through context7 at v4.1.3, upgrade guide at v4.1.4), Pydantic AI 2.57.0, Python 3.14.6.

**Local probes.** A claim marked *(probe)* was also run on both FastMCP majors with `pydantic-ai-slim` 2.57.0, in throwaway environments, with `TestModel` or `FunctionModel`: no network, no paid model call, no athlete data. A probe only confirms what its cited source says. The scripts and their output are in the PR for #369.

## Questions

1. How should tools be named and described, and what should they return?
2. How many tools can one agent carry?
3. How should a tool report errors, and what does `ToolResult[T]` mean against the specification?
4. How does identity reach a tool safely, in process and later over HTTP?
5. How do Pydantic AI specialists consume the server in process?
6. How are tools tested?
7. FastMCP 3 or 4, with `pydantic-ai-slim`'s `mcp` extra on Python 3.14?

## Findings

### 1. Naming, descriptions and result shapes

- Tool names SHOULD be 1–128 characters from ASCII letters, digits, `_`, `-` and `.`, case-sensitive and unique within a server ([MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), 2026-07-28).
- Claude's API accepts only `^[a-zA-Z0-9_-]{1,128}$`, so a dot that the specification allows is refused there ([Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools), read 2026-10-31).
- Anthropic calls detailed descriptions "by far the most important factor in tool performance" and asks for at least 3–4 sentences: what the tool does, when to use it and when not, what each parameter means, and its limits ([Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools), read 2026-10-31).
- Parameters should be named without ambiguity, and inputs and outputs enforced with strict data models ([Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents), published 2025-09-11).
- Tools should return only high-signal information, resolve opaque identifiers to natural names, and keep the identifiers a follow-up call needs ([Writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09-11).
- Large results call for some mix of pagination, range selection, filtering and truncation, and a truncated result should tell the agent what to do next ([Writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09-11).
- A tool with an output schema MUST return conforming structured results, and SHOULD also return the serialized JSON in a text block ([MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), 2026-07-28).
- FastMCP generates the output schema from the return annotation and always returns both forms for a Pydantic model ([FastMCP tools](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/tools.mdx), v4.1.3). *(probe: a generic `Envelope[Row]` produced a schema with its four fields on both majors, and the model saw the JSON.)*
- `ToolResult` is already taken in both libraries: `fastmcp.tools.ToolResult` ([upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4) and `pydantic_ai.mcp.ToolResult` ([Pydantic AI client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0).

### 2. Tool count

- Anthropic gives no maximum in its tool-design article. It says more tools don't always help, recommends a few tools aimed at high-impact workflows, and warns that too many or overlapping tools distract agents ([Writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09-11).
- It does state a figure elsewhere, for Claude only and with no data on the page: selection "degrades once you exceed 30–50 available tools", with tool search suggested from 10 tools up ([Tool search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool), read 2026-10-31).
- It also advises consolidating related operations into fewer tools, and prefixing names by service as a library grows ([Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools), read 2026-10-31).
- Pydantic AI disambiguates several servers with `.prefixed(...)` ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0).
- Neither [the tool-design article](https://www.anthropic.com/engineering/writing-tools-for-agents) nor [the Claude docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool) (read 2026-10-31) supports the "roughly 10–20" figure in ADR-0010's Consequences. Their one figure is about Claude, so it doesn't settle this project's models (see Open questions).

### 3. Errors

- The specification has two mechanisms. Protocol errors are JSON-RPC errors for unknown tools, malformed requests and server errors. Tool execution errors are results with `isError: true` for API failures, input validation and business logic ([MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), 2026-07-28).
- Clients SHOULD pass tool execution errors to the model so it can self-correct, and MAY pass protocol errors ([MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), 2026-07-28).
- **A raised exception does not become a protocol error in FastMCP.** `call_tool` wraps any tool exception in `ToolError`, and the handler returns that, and an unknown tool too, as an `is_error=True` result ([`server.py`](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/fastmcp_slim/fastmcp/server/server.py#L1562-L1564), [`mcp_operations.py`](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/fastmcp_slim/fastmcp/server/mixins/mcp_operations.py#L252-L272), v4.1.4). *(probe: on both majors a `ValueError`, a `ToolError`, the SDK's `McpError` raised in a tool and an unknown tool name all returned a result with the error flag set.)*
- `Client.call_tool` still raises `ToolError` on such a result unless `raise_on_error=False` is passed ([FastMCP client tools](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/clients/tools.mdx), v4.1.3).
- `MCPToolset.tool_error_behavior` decides what the model sees: `'retry'` (default) sends the error back as a retry prompt, `'failed'` records a failed tool result, and `'error'` fails the run ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0).
- For a tool that raises on missing identity, `'retry'` retries within the run's limit, then fails it; `'failed'` hands the exception text to the model and the run goes on; `'error'` raises `ToolError` ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0). *(probe, both majors: `'retry'` ran the tool twice, then raised `UnexpectedModelBehavior`, and `'error'` raised after one run.)*
- Schema-invalid arguments come back as a tool error too ([`mcp_operations.py`](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/fastmcp_slim/fastmcp/server/mixins/mcp_operations.py#L252-L272), v4.1.4), so `'retry'` lets the model correct them and `'error'` fails the whole run ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0). So `'error'` can't protect identity without losing recovery from bad input. *(probe, both majors: exactly this, and the validation message reached the model even with `mask_error_details=True`.)*
- With `mask_error_details=True`, only `ToolError` messages reach clients and other exceptions become a generic message ([FastMCP tools](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/tools.mdx), v4.1.3). The default is off ([settings](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/more/settings.mdx), v4.1.3).
- Anthropic asks for error responses that state specific, actionable improvements rather than opaque codes or tracebacks ([Writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents), 2025-09-11).
- The specification says business-logic errors SHOULD use `isError` ([schema](https://modelcontextprotocol.io/specification/2026-07-28/schema), 2026-07-28), so a `success=False` envelope in a normal result departs from that SHOULD. *(probe: the envelope reached the model unchanged, used no retry, and the run went on.)*

### 4. Identity and tenancy

- `process_tool_call` is the documented way to inject request metadata, and `metadata` is keyword-only: `call_tool(name, tool_args, metadata={...})` ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0). The v2.0.0 docs passed it positionally ([v2.0.0](https://github.com/pydantic/pydantic-ai/blob/v2.0.0/docs/mcp/client.md)). *(probe: the positional form raises `TypeError` on 2.57.0.)*
- The shape of `ctx.request_context.meta` differs by major: an object with attribute access on FastMCP 3 ([v3.4.11](https://github.com/PrefectHQ/fastmcp/blob/v3.4.11/docs/servers/context.mdx)), a plain `dict` or `None` on FastMCP 4, where attribute access raises `AttributeError` ([v4.1.3](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/context.mdx)). Pydantic AI's own server example still uses `getattr`, against the MCP SDK ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0). *(probe: the user ID injected through the hook reached the tool on both majors, through each major's own access form.)*
- On FastMCP 4 the dictionary also holds protocol keys such as `progressToken` ([v4.1.3](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/context.mdx)). Prefixes whose second label is `modelcontextprotocol` or `mcp` are reserved, the prefix is optional, and `user_id` is a valid name that isn't on the reserved list ([MCP basic](https://modelcontextprotocol.io/specification/2026-07-28/basic), 2026-07-28).
- `metadata` comes from the caller ([v4.1.3](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/context.mdx)), so it is only as trustworthy as the host that sent it.
- A host hook that raises before calling the tool fails the run on the spot ([`mcp.py`](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/mcp.py#L1522-L1525), v2.57.0). *(probe: the exception surfaced unchanged, the tool never ran, and the model got no retry.)*
- Over HTTP, servers MUST validate access tokens, MUST reject tokens not issued for them, and MUST NOT pass a client's token upstream ([authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization), [security considerations](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations), 2026-07-28).
- `get_access_token()` returns the current token or `None`, and it stays available in FastMCP 4 ([authorization](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/authorization.mdx), v4.1.3).
- `http_app` with `combine_lifespans` is still the documented FastAPI mount in FastMCP 4 ([FastAPI integration](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/integrations/fastapi.mdx), v4.1.3).
- FastMCP's OpenTelemetry instrumentation is on by default and only records once an SDK is configured. A raised exception's message becomes the span's status description ([telemetry](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/telemetry.mdx), v4.1.3). `FASTMCP_TELEMETRY_MODE` can limit or disable those spans ([v4 change register](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/dev-docs/v4-notes/change-register.md), v4.1.3).

### 5. In-process toolsets

- `MCPToolset(server)` takes a FastMCP instance in the same process and saves the network round trip ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0; [FastMCP integration](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/integrations/pydantic-ai.mdx), v4.1.3).
- The docs recommend the `MCP` capability for most uses, and `MCPToolset` directly when attaching one server to several agents or passing advanced configuration ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0).
- The `mcp` extra installs only the client, `fastmcp-slim[client]`, so an environment that also runs a server has to add `fastmcp` ([FastMCP integration](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/integrations/pydantic-ai.mdx), v4.1.3).
- On FastMCP 4, `Client` negotiates the sessionless `2026-07-28` era by default. There, `on_initialize` never runs, `ctx.set_state` doesn't persist across calls, and `ctx.elicit()` raises ([upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4).

### 6. Testing

- `Client(server)` runs real FastMCP dispatch and protocol behaviour without a socket, and is the documented way to test ([FastMCP tests](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/development/tests.mdx), v4.1.3).
- `Client.call_tool(..., meta={...})` sends metadata from a test, and `raise_on_error=False` lets a test read `is_error` ([client tools](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/clients/tools.mdx), v4.1.3).
- `TestModel(call_tools=[...])` drives a real `process_tool_call` path with no model behind it ([client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0). *(probe: this ran with model requests disallowed.)*

### 7. FastMCP 3 or 4

| | FastMCP 3 | FastMCP 4 | Source |
|---|---|---|---|
| Latest stable on 2026-10-31 | 3.4.11 (2026-10-20) | 4.1.4 (2026-10-26) | [PyPI](https://pypi.org/project/fastmcp/#history) |
| First stable | 3.0.0 (2026-02-18) | 4.0.0 (2026-08-31), so about two months old | [PyPI](https://pypi.org/project/fastmcp/#history) |
| `pydantic-ai-slim` 2.57's `mcp` extra | Accepted (`>=3.3.0,<5`) | Accepted, and installed by default | [client docs](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md), v2.57.0 |
| MCP SDK | v1 (`mcp` 1.30.0 resolved) | v2 (`mcp` 2.3.0 resolved) | [upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4 |
| HTTP client | Adds `httpx` | Uses `httpx2`, which this project already locks | [upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4 |
| Requires | | pydantic ≥2.12, FastAPI ≥0.133, `pydantic-ai-slim` ≥2.45. This project locks pydantic 2.13.5, FastAPI 0.141.1 and `pydantic-ai-slim` 2.57.0 | [upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4 |
| `ctx.request_context.meta` | Object, attribute access | Plain `dict` or `None` | [v3.4.11](https://github.com/PrefectHQ/fastmcp/blob/v3.4.11/docs/servers/context.mdx), [v4.1.3](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/context.mdx) |
| Protocol | Session-based eras | Serves both the sessionless `2026-07-28` era and session-based eras | [upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4 |
| Moving later | The upgrade guide's migrations are still ahead | Nothing to migrate | [upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4 |

- FastMCP's removals in 4.0 (`ctx.sample`, `ctx.list_roots`, session state on the modern era) touch nothing ADR-0010 plans ([upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4).
- The guide says 3.x remains the right home only for a server that borrows its caller's model through sampling, which this project doesn't do: model calls go through `OpenRouterGateway` ([upgrade guide](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx), v4.1.4; ADR-0007).
- 3.x still gets releases, but no source read here states how long it will ([PyPI](https://pypi.org/project/fastmcp/#history), read 2026-10-31).
- 4.x moves fast: 4.0.0 to 4.1.4 took 17 releases in two months, and an alpha, 4.2.0a1, is on PyPI ([PyPI](https://pypi.org/project/fastmcp/#history), read 2026-10-31). `uv.lock` pins the exact version, and the lock check below chose 4.1.4, not the alpha.

*Lock check (2026-10-31), on a scratch copy of this repository's `pyproject.toml` and `uv.lock`:* both majors resolve on Python 3.14.6 with no package removed, and only `certifi` moves among existing ones. FastMCP 3 adds 42 packages, including `httpx`, `httpcore` and `keyring`, for 147 in all. FastMCP 4 adds 32 for 137 in all. *(probe: every in-process check above gave the same result on both majors, apart from the shape of `meta`.)*

## Recommendations for this project

- **R1 (Q1), tool design.** Use `snake_case` names with no dots, as ADR-0010 already does. Each description says what the tool does, when to use it and when not, and its limits, in 3–4 sentences. Every list-returning tool takes a bounded `limit` and says when its result was cut. Return names the model can use, plus the IDs a follow-up tool needs, and no internal fields. Import ADR-0010's `ToolResult[T]` from our own module, and never import a library's `ToolResult` next to it.
- **R2 (Q2), tool count.** Keep ADR-0010's 5–15 range as this project's ceiling, because it sits well below the only published figure and the sources favour few tools. Scope tools per specialist (ADR-0008) before adding more. Don't merge reads and draft writes behind one `action` parameter, because invariant 2 is easier to check on a separate write tool. Add no prefix while there is one server.
- **R3 (Q3), errors.** Keep the `ToolResult[T]` envelope for domain failures: it reaches the model unchanged, uses no retry, and can lead to a choice card (ADR-0015). It departs from the specification's SHOULD for business-logic errors, which matters only to external clients, so revisit it in the HTTP amendment. Keep `tool_error_behavior` at its default `'retry'`, so bad arguments stay recoverable. Set `mask_error_details=True`.
- **R3a (Q3), contradicts ADR-0010, needs `/update-docs`.** ADR-0010 says identity failures "raise, becoming a protocol-level MCP error". Neither major does that. The goal, that the model never retries around them, holds only with R4's guard in the host. Proposal: change the ADR to say that identity failures raise, fail closed and never return a `ToolResult`; the host's hook stops the run before the call; a test asserts `Client.call_tool` raises `ToolError`. #378's acceptance criterion "raises a protocol-level error" should change to match.
- **R4 (Q4), identity.** Guard identity twice. The `process_tool_call` hook raises an application exception when deps carry no user, so the run stops with no retry. `current_user_id(ctx)` then raises when `meta` has no user, so a bypassed hook fails closed. Use one key constant (`user_id`), read with `meta.get`, sent with `metadata=`. Exception messages carry no athlete data, since they can reach the model and traces (ADR-0018). Treat `meta` as trusted only in process, as ADR-0010 says. The HTTP amendment must require audience-checked tokens.
- **R5 (Q5), in-process.** Follow ADR-0010: `MCPToolset(server, process_tool_call=...)`, with `fastmcp` as a direct dependency next to `pydantic-ai-slim[mcp]`, because the extra alone is client-only. Don't rely on `ctx.set_state`, `on_initialize` or `ctx.elicit()`: ambiguity is a choice card anyway (ADR-0015).
- **R6 (Q6), testing.** Keep ADR-0010's four tests through `Client(server)`, and add one `TestModel` run that goes through the real hook, because that is the only path where the hook runs. Also test that schema-invalid arguments leave the run recoverable.
- **R7 (Q7), version.** Choose **FastMCP 4**: `fastmcp>=4.1,<5`, with the exact version in `uv.lock`. It is what Pydantic AI 2.57 installs, it brings fewer packages and no second HTTP client, and it avoids a migration later. Every in-process check gave the same result as on 3, and nothing it removed is planned here. The cost is a young major that changes quickly: take updates deliberately, through `uv.lock`, not automatically.

## Open questions

- **Do tool descriptions count as prompt text?** ADR-0008 invariant 1 bans prompt text in Python strings outside tests, while `/add-mcp-tool` makes the docstring the model-facing description, and no doc reconciles them. The sources don't settle a project rule. To be settled by the owner, at this ticket's PR review or in #378's plan; until then the skill's docstring stands.
- **Where does tool selection degrade on this project's models?** The only published figure is for Claude. To be settled by smoke sets (ADR-0034) if a specialist starts picking the wrong tools.
- **Do FastMCP or Pydantic AI spans carry the user ID or athlete text when telemetry is on?** The sources show exception messages become span descriptions, but they don't list every attribute, and ADR-0018 requires content-free traces. To be settled by a test in #378 with an in-memory exporter, choosing `FASTMCP_TELEMETRY_MODE` from the result.
- **What should a specialist do when retries run out?** After `'retry'` is exhausted, the run raises `UnexpectedModelBehavior` *(probe)*. The sources don't say how this project should present that to an athlete. To be settled in the first ticket that wires a tool into a specialist.
- **How long will FastMCP 3 be maintained?** No source states it. It matters only if FastMCP 4 has to be abandoned, and would be settled by then-current release notes.
- **What does an in-process call cost in latency on the sessionless era?** No source gives a figure. ADR-0010 keeps the logging hot path off MCP, so this doesn't block anything. To be measured with the latency scenario (ADR-0026) if a specialist's tool loop proves slow.
- **How are tokens issued for external clients?** ADR-0010 defers this to an amendment. That amendment also decides whether external clients get the `success=False` envelope or `isError`.

## Sources

| Source | Kind | Version or date read |
|---|---|---|
| [MCP specification: tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) | Spec | 2026-07-28 |
| [MCP specification: schema](https://modelcontextprotocol.io/specification/2026-07-28/schema) | Spec | 2026-07-28 |
| [MCP specification: basic, `_meta`](https://modelcontextprotocol.io/specification/2026-07-28/basic) | Spec | 2026-07-28 |
| [MCP specification: authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) | Spec | 2026-07-28 |
| [MCP specification: authorization security considerations](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations) | Spec | 2026-07-28 |
| [FastMCP: upgrading from FastMCP 3](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx) | Official docs | v4.1.4 |
| [FastMCP: tools](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/tools.mdx) | Official docs | v4.1.3 |
| [FastMCP: context](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/context.mdx) | Official docs | v4.1.3 |
| [FastMCP: context, v3](https://github.com/PrefectHQ/fastmcp/blob/v3.4.11/docs/servers/context.mdx) | Official docs | v3.4.11 |
| [FastMCP: client tools](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/clients/tools.mdx) | Official docs | v4.1.3 |
| [FastMCP: settings](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/more/settings.mdx) | Official docs | v4.1.3 |
| [FastMCP: authorization](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/authorization.mdx) | Official docs | v4.1.3 |
| [FastMCP: FastAPI integration](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/integrations/fastapi.mdx) | Official docs | v4.1.3 |
| [FastMCP: Pydantic AI integration](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/integrations/pydantic-ai.mdx) | Official docs | v4.1.3 |
| [FastMCP: telemetry](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/telemetry.mdx) | Official docs | v4.1.3 |
| [FastMCP: tests](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/development/tests.mdx) | Official docs | v4.1.3 |
| [FastMCP: v4 change register](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/dev-docs/v4-notes/change-register.md) | Upstream code | v4.1.3 |
| [FastMCP: `server.py`](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/fastmcp_slim/fastmcp/server/server.py#L1562-L1564) | Upstream code | v4.1.4 |
| [FastMCP: `mcp_operations.py`](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/fastmcp_slim/fastmcp/server/mixins/mcp_operations.py#L252-L272) | Upstream code | v4.1.4 |
| [FastMCP release history](https://pypi.org/project/fastmcp/#history) | Upstream release notes | Read 2026-10-31 |
| [Pydantic AI: MCP client](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md) | Official docs | v2.57.0 |
| [Pydantic AI: MCP client, v2.0.0](https://github.com/pydantic/pydantic-ai/blob/v2.0.0/docs/mcp/client.md) | Official docs | v2.0.0 |
| [Pydantic AI: `mcp.py`](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/mcp.py#L1522-L1525) | Upstream code | v2.57.0 |
| [Anthropic: Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) | Maker's engineering | Published 2025-09-11, read 2026-10-31 |
| [Claude docs: Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools) | Official docs | Read 2026-10-31 |
| [Claude docs: Tool search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool) | Official docs | Read 2026-10-31 |
