"""`ai-trainer storage-check` against an S3 server on 127.0.0.1 (ADR-0033, ticket #420)."""

import io
import logging
from collections.abc import Iterator

import pytest

from ai_trainer import cli
from ai_trainer.application import storage_check
from ai_trainer.cli_knowledge import STORAGE_CHECK
from tests.object_storage_support import (
    ACCESS_KEY_ID,
    BUCKET,
    SECRET_ACCESS_KEY,
    S3State,
    s3_server,
)


def _configure(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT", endpoint)
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY_ID", ACCESS_KEY_ID)
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_ACCESS_KEY", SECRET_ACCESS_KEY)
    monkeypatch.setenv("OBJECT_STORAGE_UPLOADS_BUCKET", BUCKET)
    monkeypatch.setenv("OBJECT_STORAGE_TIMEOUT_SECONDS", "5")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3State]:
    with s3_server() as state:
        _configure(monkeypatch, state.endpoint)
        yield state


async def test_cli_round_trip_against_an_s3_server(server: S3State) -> None:
    async def the_link_runs_out(seconds: float) -> None:
        server.seconds_after_signing = storage_check.LINK_SECONDS + 1

    output = io.StringIO()

    code = await cli.run([STORAGE_CHECK], output=output, sleep=the_link_runs_out)

    assert code == 0
    assert output.getvalue() == (
        f"storage-check: result=ok bucket={BUCKET} steps=put,read,link,expiry,delete\n"
    )
    assert server.objects == {}


async def test_cli_failure_prints_no_key(server: S3State, caplog: pytest.LogCaptureFixture) -> None:
    server.refuse_with = 403
    output = io.StringIO()

    with caplog.at_level(logging.DEBUG):
        code = await cli.run([STORAGE_CHECK], output=output)

    assert code == 1
    assert output.getvalue() == (
        "storage-check: result=failed step=put reason=denied probe_left=false\n"
    )
    for text in (output.getvalue(), caplog.text):
        assert ACCESS_KEY_ID not in text
        assert SECRET_ACCESS_KEY not in text


async def test_cli_says_when_a_probe_is_left_behind(server: S3State) -> None:
    async def service_starts_refusing(seconds: float) -> None:
        server.refuse_with = 403

    output = io.StringIO()

    code = await cli.run([STORAGE_CHECK], output=output, sleep=service_starts_refusing)

    assert code == 1
    assert output.getvalue() == (
        "storage-check: result=failed step=delete reason=denied probe_left=true\n"
    )
    assert len(server.objects) == 1


async def test_cli_without_storage_settings_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "OBJECT_STORAGE_ENDPOINT",
        "OBJECT_STORAGE_ACCESS_KEY_ID",
        "OBJECT_STORAGE_SECRET_ACCESS_KEY",
        "OBJECT_STORAGE_UPLOADS_BUCKET",
    ):
        monkeypatch.setenv(key, "")
    output = io.StringIO()

    code = await cli.run([STORAGE_CHECK], output=output)

    assert code == 1
    assert output.getvalue() == "storage-check: result=failed reason=not_configured\n"
