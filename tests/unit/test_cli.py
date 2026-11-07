"""`ai-trainer` console script: `purge-conversations` prints a count and nothing of what was
deleted (ADR-0021, ticket #148); `llm-debug-report` summarizes the debug log (#179)."""

import io
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ai_trainer import cli, cli_knowledge
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryChatRepository


async def test_purge_prints_only_the_outcome_line_with_the_count_of_deleted_conversations() -> None:
    chat = InMemoryChatRepository()
    await chat.conversations.create(uuid4(), "a private title", FROZEN_NOW - timedelta(days=30))
    await chat.conversations.create(uuid4(), "another title", FROZEN_NOW - timedelta(days=15))
    await chat.conversations.create(uuid4(), "live title", FROZEN_NOW)
    output = io.StringIO()

    await cli.purge_conversations(
        clock=FixedClock(), conversations=chat.conversations, retention_days=14, output=output
    )

    assert output.getvalue() == "purge: result=ok deleted=2\n"


async def test_a_second_run_prints_zero() -> None:
    chat = InMemoryChatRepository()
    await chat.conversations.create(uuid4(), "old", FROZEN_NOW - timedelta(days=30))
    first, second = io.StringIO(), io.StringIO()

    for output in (first, second):
        await cli.purge_conversations(
            clock=FixedClock(), conversations=chat.conversations, retention_days=14, output=output
        )

    assert (first.getvalue(), second.getvalue()) == (
        "purge: result=ok deleted=1\n",
        "purge: result=ok deleted=0\n",
    )


def test_the_parser_knows_purge_conversations() -> None:
    assert cli.parse_args(["purge-conversations"]).command == "purge-conversations"


def test_the_parser_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args([])

    assert excinfo.value.code == 2
    assert "command" in capsys.readouterr().err


def test_main_runs_the_command_and_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    async def fake_run(argv: Sequence[str]) -> int:
        ran.append(list(argv))
        return 0

    monkeypatch.setattr(cli, "run", fake_run)

    assert cli.main(["purge-conversations"]) == 0
    assert ran == [["purge-conversations"]]


def test_main_returns_the_commands_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(argv: Sequence[str]) -> int:
        return 1

    monkeypatch.setattr(cli, "run", fake_run)

    assert cli.main(["llm-debug-report"]) == 1


def test_the_parser_reads_since_as_an_aware_time() -> None:
    args = cli.parse_args(["llm-debug-report", "--since", "2026-10-18T22:00:00+02:00"])

    assert args.command == "llm-debug-report"
    assert args.since == datetime(2026, 10, 18, 20, tzinfo=UTC)


def test_a_since_without_a_timezone_is_utc() -> None:
    assert cli.parse_args(["llm-debug-report", "--since", "2026-10-18T20:00"]).since == datetime(
        2026, 10, 18, 20, tzinfo=UTC
    )


def test_the_report_runs_without_since() -> None:
    assert cli.parse_args(["llm-debug-report"]).since is None


def test_a_since_that_is_not_a_time_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(["llm-debug-report", "--since", "yesterday"])

    assert excinfo.value.code == 2
    assert "ISO" in capsys.readouterr().err


async def test_the_report_reads_the_settings_folder_and_returns_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "debug"
    monkeypatch.setenv("LLM_DEBUG_LOG_DIR", str(missing))

    def no_database(url: str) -> None:
        raise AssertionError("the report must not open the database")

    monkeypatch.setattr(cli, "build_engine", no_database)
    output = io.StringIO()

    assert await cli.run(["llm-debug-report"], output=output) == 1
    assert output.getvalue() == f"no debug log at {missing}\n"


class _Index:
    index = "ix_knowledge_chunks_embedding"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.asked: list[int] = []

    async def rebuild(self, *, memory_mb: int) -> str:
        self.asked.append(memory_mb)
        if self.error is not None:
            raise self.error
        return f"{memory_mb}MB"


async def test_rebuild_knowledge_index_prints_one_line() -> None:
    index = _Index()
    output = io.StringIO()

    code = await cli_knowledge.rebuild_knowledge_index(index, memory_mb=200, output=output)

    assert code == 0
    assert index.asked == [200]
    assert output.getvalue() == (
        "rebuild-knowledge-index: result=ok index=ix_knowledge_chunks_embedding"
        " maintenance_work_mem=200MB\n"
    )


async def test_a_failed_rebuild_prints_only_the_error_class() -> None:
    output = io.StringIO()

    code = await cli_knowledge.rebuild_knowledge_index(
        _Index(RuntimeError("could not resize shared memory segment on db:5432")),
        memory_mb=256,
        output=output,
    )

    assert code == 1
    assert output.getvalue() == "rebuild-knowledge-index: result=failed reason=RuntimeError\n"


def test_the_rebuild_memory_defaults_to_the_setting_and_is_bounded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.parse_args([cli_knowledge.REBUILD_KNOWLEDGE_INDEX]).memory_mb is None
    assert (
        cli.parse_args([cli_knowledge.REBUILD_KNOWLEDGE_INDEX, "--memory-mb", "256"]).memory_mb
        == 256
    )
    for value in ("63", "257"):
        with pytest.raises(SystemExit):
            cli.parse_args([cli_knowledge.REBUILD_KNOWLEDGE_INDEX, "--memory-mb", value])
    assert "shm_size" in capsys.readouterr().err
