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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model

from ai_trainer.application.exercise_lookup import ExerciseFinder
from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.training_history import ExerciseTrainingHistory, TrainingHistory
from ai_trainer.domain.exercises import (
    CatalogueExercise,
    CatalogueWrite,
    Exercise,
    ExerciseAlias,
    ExerciseCandidate,
    ExerciseDetail,
    ExerciseEntry,
    ExerciseLabel,
    ExerciseMeasure,
    LoggedName,
    LoggedSet,
)
from ai_trainer.domain.embeddings import Embedded, EmbeddingOutcome
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import Locator, SectionLocator
from ai_trainer.domain.knowledge_search import (
    KnowledgeCandidates,
    KnowledgeFilters,
    KnowledgeHit,
)
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionSummary
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.tool_loop import REQUEST_BREAK
from ai_trainer.mcp.citations import cited_sources
from ai_trainer.mcp.server import build_mcp_server
from ai_trainer.mcp.toolset import build_toolset

_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)
# The fixture's own: no chunk is filtered by it, and no embedding call is made.
_EMBEDDING_MODEL = "smoke"


class _Set(BaseModel):
    reps: int | None = None
    load_kg: float | None = None
    load_original: str | None = None
    duration_s: int | None = None
    band_label: str | None = None


class _Logged(BaseModel):
    """One logged exercise. `exercise` is the name it is linked to; none leaves it typed-only."""

    day: date
    typed: str
    exercise: str | None = None
    per_side: bool = False
    implements: int | None = None
    sets: list[_Set]


class _Expect(BaseModel):
    """`mentions` and `forbids` are regular expressions, read case-insensitively."""

    mentions: list[str] = []
    forbids: list[str] = []
    no_digits: bool = False
    called: list[str] = []
    # The titles the reply must cite, in any order; `[]` cites nothing, absent isn't graded.
    cites: list[str] | None = None


class _Passage(BaseModel):
    """One passage of a case's knowledge base."""

    title: str
    author: str | None = None
    evidence_tier: EvidenceTier
    source_type: SourceType
    locator: Locator = SectionLocator()
    language: SourceLanguage = SourceLanguage.EN
    text: str


class _Metadata(BaseModel):
    logged: list[_Logged] = []
    catalogue: list[str] = []
    knowledge: list[_Passage] = []
    expect: _Expect


class _Case(BaseModel):
    name: str
    inputs: EvalCaseInputs
    metadata: _Metadata


@dataclass(frozen=True, slots=True)
class _Deps:
    user_id: UUID


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, _norm(name))


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def _fits(one: str, other: str) -> bool:
    """Stands in for the database's word similarity: one name inside the other."""
    return _norm(one) in _norm(other) or _norm(other) in _norm(one)


class _CaseHistory:
    """Implements the ports the tools read, over one case's fixture. The case has one athlete."""

    def __init__(self, sport_id: str, logged: Sequence[_Logged], catalogue: Sequence[str]) -> None:
        self._sport_id = sport_id
        self._logged = logged
        self._catalogue = catalogue

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        sport_id: str | None,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> Sequence[SessionSummary]:
        days = sorted(
            {
                entry.day
                for entry in self._logged
                if (since is None or entry.day >= since) and (until is None or entry.day <= until)
            },
            reverse=True,
        )
        if sport_id not in (None, self._sport_id):
            return []
        return [
            SessionSummary(id=_id(str(day)), sport_id=self._sport_id, occurred_on=day)
            for day in days[:limit]
        ]

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        return {
            name: _id(entry.exercise)
            for name in names
            for entry in self._logged
            if entry.exercise is not None and _norm(name) == _norm(entry.typed)
        }

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        return [
            ExerciseCandidate(id=_id(listed), name=listed)
            for listed in self._catalogue
            if _fits(name, listed)
        ][:limit]

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> Mapping[str, ExerciseLabel]:
        return {}  # pragma: no cover -- no tool reads a draft card's labels

    async def describe(
        self, user_id: UUID, exercise_ids: Sequence[UUID], *, lang: str
    ) -> Sequence[ExerciseDetail]:
        names = {_id(name): name for name in self._exercise_names()}
        return [
            ExerciseDetail(id=exercise_id, name=names[exercise_id])
            for exercise_id in exercise_ids
            if exercise_id in names
        ]

    async def get(self, user_id: UUID, exercise_id: UUID) -> Exercise | None:
        for name in self._exercise_names():
            if _id(name) == exercise_id:
                return Exercise(
                    id=exercise_id,
                    sport_id=self._sport_id,
                    owner_user_id=None,
                    name=name,
                    default_measure=ExerciseMeasure.LOAD_REPS,
                )
        return None

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return self._read(
            [e for e in self._logged if e.exercise is not None and _id(e.exercise) == exercise_id],
            limit,
        )

    async def typed_entries(
        self, user_id: UUID, typed_name: str, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return self._read(
            [
                entry
                for entry in self._logged
                if entry.exercise is None and _norm(entry.typed) == _norm(typed_name)
            ],
            limit,
        )

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        counts: dict[tuple[UUID | None, str], int] = {}
        for entry in self._logged:
            if not (_fits(name, entry.typed) or (entry.exercise and _fits(name, entry.exercise))):
                continue
            key = (
                (_id(entry.exercise), entry.exercise)
                if entry.exercise is not None
                else (None, _norm(entry.typed))
            )
            counts[key] = counts.get(key, 0) + 1
        return [
            LoggedName(exercise_id=exercise_id, name=logged, entries=count)
            for (exercise_id, logged), count in counts.items()
        ][:limit]

    def _exercise_names(self) -> list[str]:
        linked = [entry.exercise for entry in self._logged if entry.exercise is not None]
        return [*linked, *self._catalogue]

    def _read(self, found: Sequence[_Logged], limit: int) -> list[ExerciseEntry]:
        latest_first = sorted(found, key=lambda entry: entry.day, reverse=True)
        return [
            ExerciseEntry(
                session_id=_id(str(entry.day)),
                occurred_on=entry.day,
                name_as_typed=entry.typed,
                measure=ExerciseMeasure.LOAD_REPS,
                per_side=entry.per_side,
                implements=entry.implements,
                sets=tuple(
                    LoggedSet(set_no=number, **logged_set.model_dump())
                    for number, logged_set in enumerate(entry.sets, start=1)
                ),
            )
            for entry in latest_first[:limit]
        ]

    async def upsert_catalogue(self, exercises: Sequence[CatalogueExercise]) -> CatalogueWrite:
        raise NotImplementedError  # pragma: no cover -- no tool writes the catalogue

    async def create_custom(
        self, user_id: UUID, *, sport_id: str, name: str, default_measure: ExerciseMeasure
    ) -> Exercise:
        raise NotImplementedError  # pragma: no cover -- no tool creates an exercise

    async def add_user_alias(
        self, user_id: UUID, exercise_id: UUID, alias: str
    ) -> ExerciseAlias | None:
        raise NotImplementedError  # pragma: no cover -- no tool writes an alias

    async def user_aliases(self, user_id: UUID) -> Sequence[ExerciseAlias]:
        raise NotImplementedError  # pragma: no cover -- no tool reads every alias


class _CaseKnowledge:
    """Implements the search's ports over one case's passages: every passage comes back by
    meaning, in the case's order, under any filter."""

    def __init__(self, passages: Sequence[_Passage]) -> None:
        self._hits = tuple(
            KnowledgeHit(
                chunk_id=_id(f"{passage.title}/{number}"),
                text=passage.text,
                heading_path=(),
                language=passage.language,
                source_id=_id(passage.title),
                title=passage.title,
                author=passage.author,
                evidence_tier=passage.evidence_tier,
                source_type=passage.source_type,
                source_url=None,
                attribution=None,
                locator=passage.locator,
                exercise_id=None,
            )
            for number, passage in enumerate(passages)
        )

    async def embed_query(self, text: str, *, acting_user_id: UUID) -> EmbeddingOutcome:
        return Embedded(vectors=((1.0,),), model=_EMBEDDING_MODEL)

    async def candidates(
        self, *, vector: Sequence[float], query: str, filters: KnowledgeFilters, depth: int
    ) -> KnowledgeCandidates:
        return KnowledgeCandidates(by_meaning=self._hits[:depth], by_words=())


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
        fake = _CaseHistory(sport_id, case.metadata.logged, case.metadata.catalogue)
        knowledge = _CaseKnowledge(case.metadata.knowledge)
        server = build_mcp_server(
            TrainingHistory(fake, default_sport_registry()),
            ExerciseFinder(fake, _LIMITS, fake),
            ExerciseTrainingHistory(fake, fake),
            knowledge=KnowledgeSearch(
                embeddings=knowledge,
                repository=knowledge,
                sports=default_sport_registry(),
                embedding_model=_EMBEDDING_MODEL,
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
