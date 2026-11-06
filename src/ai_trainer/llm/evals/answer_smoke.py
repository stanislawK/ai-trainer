"""The answer templates' smoke sets (ADR-0034, ticket #421), such as `evals/smoke/gym.answer.yaml`.
A case gives what the athlete logged, the question and what the reply must and must not say. It
runs on the real MCP server and identity hook, so the model reads the descriptions the app
publishes, over an in-memory history built from the case. Graded in code, once, with no judge. No
`llm_calls` row: no user is acting, and a case holds no real athlete's identity.

A case may carry `knowledge` passages, which `search_knowledge` returns whatever the query, as the
app's search does with no relevance floor yet (ADR-0036 #424). `cites` grades the titles the reply
showed, read from the run as the app reads them (ADR-0036 #428). With no passages the knowledge
base is empty, so a search finds nothing."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model

from ai_trainer.application.exercise_lookup import ExerciseFinder
from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.training_history import ExerciseTrainingHistory, TrainingHistory
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals.answer_fixtures import (
    EMBEDDING_MODEL,
    CaseHistory,
    CaseKnowledge,
    LoggedFixture,
    PassageFixture,
)
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.tool_loop import REQUEST_BREAK
from ai_trainer.mcp.citations import cited_sources
from ai_trainer.mcp.server import build_mcp_server
from ai_trainer.mcp.toolset import build_toolset

_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)


class _Expect(BaseModel):
    """`mentions` and `forbids` are regular expressions, read case-insensitively."""

    mentions: list[str] = []
    forbids: list[str] = []
    no_digits: bool = False
    called: list[str] = []
    # The titles the reply must cite, in any order; `[]` cites nothing, absent isn't graded.
    cites: list[str] | None = None


class _Metadata(BaseModel):
    logged: list[LoggedFixture] = []
    catalogue: list[str] = []
    knowledge: list[PassageFixture] = []
    expect: _Expect


class _Case(BaseModel):
    name: str
    inputs: EvalCaseInputs
    metadata: _Metadata


@dataclass(frozen=True, slots=True)
class _Deps:
    user_id: UUID


def _read_cases(smoke_set: SmokeSet) -> list[_Case]:
    """Every case checked before any call, so a typo in a fixture costs nothing."""
    raw_cases = smoke_set.dataset["cases"]
    assert isinstance(raw_cases, list)
    try:
        cases = [_Case.model_validate(raw) for raw in raw_cases]
    except ValidationError as error:
        raise SmokeError(f"{smoke_set.id}: {error}") from error
    for case in cases:
        for pattern in (*case.metadata.expect.mentions, *case.metadata.expect.forbids):
            try:
                re.compile(pattern)
            except re.error as error:
                raise SmokeError(f"{smoke_set.id}: {case.name!r}: {error}") from error
    return cases


def _called(messages: Sequence[ModelMessage]) -> set[str]:
    return {
        part.tool_name
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }


def _shown(messages: Sequence[ModelMessage]) -> str:
    """The text of every model request, as the app streams and saves it (`stream_run_text`):
    a "Let me check." before the tools is graded too, not only the last request's text."""
    said = [
        spoken
        for message in messages
        if isinstance(message, ModelResponse)
        if (spoken := "".join(p.content for p in message.parts if isinstance(p, TextPart))).strip()
    ]
    return REQUEST_BREAK.join(said)


def _sport_of(template_id: str) -> str:
    """The sport whose plugin names this answer template."""
    for plugin in default_sport_registry().all():
        if plugin.answers is not None and plugin.answers.template_id == template_id:
            return plugin.id
    raise SmokeError(f"no sport names {template_id!r} as its answer template")


def _misses(reply: str, called: set[str], cited: list[str], expect: _Expect) -> list[str]:
    """What the reply got wrong, in words for the run's output."""
    misses = [
        f"no mention of /{pattern}/"
        for pattern in expect.mentions
        if not re.search(pattern, reply, re.IGNORECASE)
    ]
    misses += [
        f"says /{pattern}/"
        for pattern in expect.forbids
        if re.search(pattern, reply, re.IGNORECASE)
    ]
    if expect.no_digits and re.search(r"\d", reply):
        misses.append("gives a number")
    misses += [f"never called {tool}" for tool in expect.called if tool not in called]
    if expect.cites is not None and sorted(set(cited)) != sorted(expect.cites):
        misses.append(f"cites {sorted(set(cited))}, not {sorted(expect.cites)}")
    return misses


async def run_answer_smoke(
    smoke_set: SmokeSet,
    model: Model,
    *,
    registry: PromptRegistry,
    version: int,
    max_requests: int,
) -> list[SmokeCaseResult]:
    """Runs each case once, in order. A case whose call fails fails alone."""
    sport_id = _sport_of(smoke_set.template_id)
    template = registry.get(smoke_set.template_id, version)
    results: list[SmokeCaseResult] = []
    for case in _read_cases(smoke_set):
        fake = CaseHistory(sport_id, case.metadata.logged, case.metadata.catalogue)
        knowledge = CaseKnowledge(case.metadata.knowledge)
        server = build_mcp_server(
            TrainingHistory(fake, default_sport_registry()),
            ExerciseFinder(fake, _LIMITS, fake),
            ExerciseTrainingHistory(fake, fake),
            knowledge=KnowledgeSearch(
                embeddings=knowledge,
                repository=knowledge,
                sports=default_sport_registry(),
                embedding_model=EMBEDDING_MODEL,
                depth=20,
            ),
        )
        agent: Agent[_Deps, str] = Agent(model, deps_type=_Deps, toolsets=[build_toolset(server)])
        deps = template.deps_type.model_validate(case.inputs.deps)
        try:
            result = await agent.run(
                case.inputs.prompt,
                deps=_Deps(uuid4()),
                instructions=registry.render_instructions(template.id, version, deps),
                usage_limits=UsageLimits(request_limit=max_requests),
            )
        except Exception as error:
            results.append(_result(smoke_set, case.name, f"the call failed: {error}"))
            continue
        messages = result.all_messages()
        reply = _shown(messages)
        cited = [source.title for source in cited_sources(messages)]
        misses = _misses(reply, _called(messages), cited, case.metadata.expect)
        detail = f"{'; '.join(misses)}; got {reply!r}" if misses else ""
        results.append(_result(smoke_set, case.name, detail))
    return results


def _result(smoke_set: SmokeSet, name: str, detail: str) -> SmokeCaseResult:
    return SmokeCaseResult(
        name=name, passed=not detail, detail=detail, gap=smoke_set.known_gaps.get(name)
    )
