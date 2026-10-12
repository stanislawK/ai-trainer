"""`ai-trainer` console script (ADR-0021, ticket #148): `purge-conversations` prints a count and
nothing of what was deleted."""

import io
from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

import pytest

from ai_trainer import cli
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryChatRepository


async def test_purge_prints_only_the_count_of_deleted_conversations() -> None:
    chat = InMemoryChatRepository()
    await chat.conversations.create(uuid4(), "a private title", FROZEN_NOW - timedelta(days=30))
    await chat.conversations.create(uuid4(), "another title", FROZEN_NOW - timedelta(days=15))
    await chat.conversations.create(uuid4(), "live title", FROZEN_NOW)
    output = io.StringIO()

    await cli.purge_conversations(
        clock=FixedClock(), conversations=chat.conversations, retention_days=14, output=output
    )

    assert output.getvalue() == "2\n"


async def test_a_second_run_prints_zero() -> None:
    chat = InMemoryChatRepository()
    await chat.conversations.create(uuid4(), "old", FROZEN_NOW - timedelta(days=30))
    first, second = io.StringIO(), io.StringIO()

    for output in (first, second):
        await cli.purge_conversations(
            clock=FixedClock(), conversations=chat.conversations, retention_days=14, output=output
        )

    assert (first.getvalue(), second.getvalue()) == ("1\n", "0\n")


def test_the_parser_knows_purge_conversations() -> None:
    assert cli.parse_args(["purge-conversations"]).command == "purge-conversations"


def test_the_parser_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args([])

    assert excinfo.value.code == 2
    assert "command" in capsys.readouterr().err


def test_main_runs_the_command_and_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    async def fake_run(argv: Sequence[str]) -> None:
        ran.append(list(argv))

    monkeypatch.setattr(cli, "run", fake_run)

    assert cli.main(["purge-conversations"]) == 0
    assert ran == [["purge-conversations"]]
