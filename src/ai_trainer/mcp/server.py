"""The FastMCP server the app's specialists use in process (ADR-0010). Each tool calls one
application use case and holds no business logic; its docstring is the description FastMCP
publishes."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.server.context import Context
from pydantic import Field

from ai_trainer.application.exercise_lookup import (
    ExerciseFinder,
    ExerciseLookup,
    ExerciseLookupFailed,
)
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.training_history import (
    DEFAULT_ENTRIES,
    DEFAULT_SESSIONS,
    MAX_ENTRIES,
    MAX_SESSIONS,
    ExerciseHistoryFailed,
    ExerciseHistoryPage,
    ExerciseTrainingHistory,
    SessionPage,
    SessionQuery,
    SessionQueryFailed,
    TrainingHistory,
)
from ai_trainer.domain.knowledge_search import (
    DEFAULT_HITS,
    MAX_HITS,
    KnowledgeSearchFailed,
    KnowledgeSearchPage,
    KnowledgeSearchQuery,
)
from ai_trainer.mcp.identity import current_user_id
from ai_trainer.mcp.results import ToolResult

SEARCH_KNOWLEDGE_TOOL = "search_knowledge"


def build_mcp_server(
    history: TrainingHistory,
    finder: ExerciseFinder,
    exercise_log: ExerciseTrainingHistory,
    *,
    knowledge: KnowledgeSearch | None = None,
) -> FastMCP:
    """The server with its tools; `search_knowledge` only when `knowledge` is given (#424)."""
    # Only a ToolError's message reaches the client; any other exception becomes a generic
    # one, so an unexpected failure can't leak athlete data (R3).
    server = FastMCP("ai-trainer", mask_error_details=True)
    if knowledge is not None:
        _add_search_knowledge(server, knowledge)

    @server.tool
    async def query_sessions(
        ctx: Context,
        sport: str | None = None,
        since: date | None = None,
        until: date | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_SESSIONS)] = DEFAULT_SESSIONS,
    ) -> ToolResult[SessionPage]:
        """List the athlete's own saved training sessions, the latest day first, with each
        session's ID, sport, day, duration in minutes, RPE, feeling and notes. Use it to look
        back at what the athlete trained, for example before judging load or answering "what
        did I do last week"; don't use it for drafts that are not confirmed yet, or for the
        exercises inside a session. `sport` is a sport ID such as `climbing`, `gym` or
        `cycling` and leaves other sports out; `since` and `until` are ISO days (YYYY-MM-DD),
        both included; `limit` is how many sessions to return, 1 to 50, default 10. When more
        sessions match than `limit`, `truncated` is true: narrow the days or raise the limit.
        A query that finds nothing comes back with `success` false and a `recovery_hint`."""
        outcome = await history.query(
            current_user_id(ctx),
            SessionQuery(sport_id=sport, since=since, until=until, limit=limit),
        )
        if isinstance(outcome, SessionQueryFailed):
            return _failed(outcome)
        return ToolResult.ok(outcome)

    @server.tool
    async def find_exercise(ctx: Context, name: str) -> ToolResult[ExerciseLookup]:
        """Find which exercise a gym exercise name means for this athlete, with each exercise's
        ID, name, equipment, target muscle and a short description. Use it to turn a name the
        athlete typed ("bench press", "wyciskanie") into an exercise ID before reading its
        history or comparing progress; don't use it to save or rename anything, it only reads.
        `name` is the exercise as the athlete wrote it. When the athlete already settled this
        name before, `source` is `history` and that one exercise comes first and alone;
        otherwise `source` is `catalogue` and up to 4 candidates follow, best first, from the
        shared catalogue and the athlete's own exercises. `logged` lists up to 4 things the
        athlete has actually logged that fit the name, each with how many entries it has: to
        read one, pass its `exercise_id` to `exercise_history`, or its `name` as `typed_name`
        when it has no ID. A catalogue candidate not in `logged` may have no history at all. A
        name that fits nothing comes back with `success` false and a `recovery_hint`."""
        outcome = await finder.find(current_user_id(ctx), name)
        if isinstance(outcome, ExerciseLookupFailed):
            return _lookup_failed(outcome)
        return ToolResult.ok(outcome)

    @server.tool
    async def exercise_history(
        ctx: Context,
        exercise_id: UUID | None = None,
        typed_name: str | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_ENTRIES)] = DEFAULT_ENTRIES,
    ) -> ToolResult[ExerciseHistoryPage]:
        """List the athlete's own past entries of one gym exercise, the latest day first, with
        each entry's day, the name as they typed it and every set's reps, load in kg (and as
        typed), time, distance and band. Use it to see how an exercise progressed; don't use it
        to find an exercise by name (call `find_exercise` first) or for sessions in general
        (`query_sessions`). Pass exactly one of `exercise_id`, an ID from `find_exercise`, and
        `typed_name`, the `name` of a `logged` row from `find_exercise` that has no ID. `limit`
        is how many entries to return, 1 to 50, default 10. When more entries exist than
        `limit`, `truncated` is true: raise the limit. An unknown ID, or an exercise or name the
        athlete has never logged, comes back with `success` false and a `recovery_hint`."""
        user_id = current_user_id(ctx)
        outcome: ExerciseHistoryPage | ExerciseHistoryFailed
        if (exercise_id is None) == (typed_name is None):
            outcome = ExerciseHistoryFailed(reason="bad_selector")
        elif exercise_id is not None:
            outcome = await exercise_log.entries(user_id, exercise_id, limit)
        else:
            assert typed_name is not None  # the first branch took the case of neither
            outcome = await exercise_log.typed_entries(user_id, typed_name, limit)
        if isinstance(outcome, ExerciseHistoryFailed):
            return _history_failed(outcome)
        return ToolResult.ok(outcome)

    return server


def _add_search_knowledge(server: FastMCP, knowledge: KnowledgeSearch) -> None:
    @server.tool(name=SEARCH_KNOWLEDGE_TOOL)
    async def search_knowledge(
        ctx: Context,
        query: str,
        *,
        sport: str | None = None,
        topics: list[str] | None = None,
        exercise_id: UUID | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_HITS)] = DEFAULT_HITS,
    ) -> ToolResult[KnowledgeSearchPage]:
        """Find passages in the shared knowledge base (research papers, coaching books,
        articles and talks an admin added) that answer a training question, by meaning and by
        exact words, best first. Use it before giving training advice that needs a source, and
        cite each passage you use by its `title`, `author`, `evidence_tier` and `locator` (a
        section, a page, an EPUB chapter or a transcript's cue times); don't use it for the
        athlete's own training, which `query_sessions` and `exercise_history` read. `query` is
        the question in the athlete's words, in English or Polish; a passage in the other
        language is still found. `sport` is a sport ID such as `climbing`, `gym` or `cycling`
        and leaves other sports' passages out; `topics` keeps passages tagged with any of them;
        `exercise_id` (from `find_exercise`) keeps only that exercise's catalogue text. `limit`
        is how many passages to return, 1 to 10, default 5; `truncated` is true when more
        matched. Nothing found, or search being unavailable, comes back with `success` false
        and a `recovery_hint`."""
        outcome = await knowledge.search(
            current_user_id(ctx),
            KnowledgeSearchQuery(
                text=query,
                sport_id=sport,
                topics=tuple(topics or ()),
                exercise_id=exercise_id,
                limit=limit,
            ),
        )
        if isinstance(outcome, KnowledgeSearchFailed):
            return _search_failed(outcome)
        return ToolResult.ok(outcome)


def _search_failed(failure: KnowledgeSearchFailed) -> ToolResult[KnowledgeSearchPage]:
    match failure.reason:
        case "blank_query":
            return ToolResult[KnowledgeSearchPage].failed(
                "The query is empty.", "Call again with the athlete's question as `query`."
            )
        case "unknown_sport":
            return ToolResult[KnowledgeSearchPage].failed(
                "Unknown sport.",
                f"Call again with one of: {', '.join(failure.known_sports)}, or no sport.",
            )
        case "no_match":
            return ToolResult[KnowledgeSearchPage].failed(
                "No passage matches.",
                "Try once more with fewer filters or the question's key words. If still "
                "nothing, the knowledge base has no source for this: say so, and don't give "
                "advice as if it were sourced.",
            )
        case "embedding_unavailable":
            return ToolResult[KnowledgeSearchPage].failed(
                "Search is unavailable right now.",
                "Don't retry now. Answer without citing, and say no source could be checked "
                "at the moment.",
            )


def _lookup_failed(failure: ExerciseLookupFailed) -> ToolResult[ExerciseLookup]:
    match failure.reason:
        case "blank_name":
            return ToolResult[ExerciseLookup].failed(
                "The name is empty.", "Call again with the exercise name the athlete wrote."
            )
        case "no_match":
            return ToolResult[ExerciseLookup].failed(
                "No exercise matches that name.",
                "Try a shorter or simpler name, or one word of it. If still nothing, the "
                "exercise isn't known: say so, and don't guess an ID.",
            )


def _history_failed(failure: ExerciseHistoryFailed) -> ToolResult[ExerciseHistoryPage]:
    match failure.reason:
        case "not_found":
            return ToolResult[ExerciseHistoryPage].failed(
                "No exercise has that ID.",
                "Call `find_exercise` with the exercise name and use an ID from its result.",
            )
        case "no_entries":
            return ToolResult[ExerciseHistoryPage].failed(
                "The athlete has not logged this exercise.",
                "Say they have no history for it yet; don't report other exercises instead.",
            )
        case "bad_selector":
            return ToolResult[ExerciseHistoryPage].failed(
                "Pass exactly one of `exercise_id` and `typed_name`.",
                "Call `find_exercise` with the exercise name, then pass one row's `exercise_id`, "
                "or its `name` as `typed_name` when it has no ID.",
            )


def _failed(failure: SessionQueryFailed) -> ToolResult[SessionPage]:
    match failure.reason:
        case "unknown_sport":
            return ToolResult[SessionPage].failed(
                "Unknown sport.",
                f"Call again with one of: {', '.join(failure.known_sports)}, or no sport.",
            )
        case "since_after_until":
            return ToolResult[SessionPage].failed(
                "`since` is after `until`.",
                "Call again with `since` on or before `until`.",
            )
        case "no_sessions":
            return ToolResult[SessionPage].failed(
                "No saved session matches.",
                "If the athlete asked about exactly these days or this sport, they logged "
                "nothing there: say so, and don't report other days instead. Widen the "
                "filters only to look further back.",
            )
