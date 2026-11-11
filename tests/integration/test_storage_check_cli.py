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
    MEDIA_BUCKET,
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
        "storage-check: media=skipped reason=no_public_url\n"
        f"storage-check: result=ok bucket={BUCKET} steps=put,private,read,link,expiry,delete\n"
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


async def _link_runs_out(server: S3State, seconds: float) -> None:
    server.seconds_after_signing = storage_check.LINK_SECONDS + 1


def _configure_media(monkeypatch: pytest.MonkeyPatch, server: S3State) -> None:
    monkeypatch.setenv("OBJECT_STORAGE_MEDIA_BUCKET", MEDIA_BUCKET)
    monkeypatch.setenv("MEDIA_PUBLIC_BASE_URL", f"{server.endpoint}/{MEDIA_BUCKET}/")


async def test_cli_checks_the_media_probe_anonymously(
    server: S3State, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_media(monkeypatch, server)
    output = io.StringIO()

    async def sleep(seconds: float) -> None:
        await _link_runs_out(server, seconds)

    code = await cli.run([STORAGE_CHECK], output=output, sleep=sleep)

    assert code == 0
    assert output.getvalue() == (
        f"storage-check: media=ok bucket={MEDIA_BUCKET} steps=media_put,media_public,media_delete\n"
        f"storage-check: result=ok bucket={BUCKET} steps=put,private,read,link,expiry,delete\n"
    )
    assert server.objects == {}


async def test_cli_fails_when_the_media_probe_is_not_public(
    server: S3State, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_media(monkeypatch, server)
    server.media_public = False
    output = io.StringIO()

    async def sleep(seconds: float) -> None:
        await _link_runs_out(server, seconds)

    code = await cli.run([STORAGE_CHECK], output=output, sleep=sleep)

    assert code == 1
    assert output.getvalue() == (
        "storage-check: result=failed step=media_public reason=status_403 probe_left=false\n"
    )
    assert server.objects == {}


async def test_cli_fails_when_uploads_can_be_read_anonymously(server: S3State) -> None:
    server.uploads_public = True
    output = io.StringIO()

    code = await cli.run([STORAGE_CHECK], output=output)

    assert code == 1
    assert output.getvalue() == (
        "storage-check: result=failed step=private reason=status_200 probe_left=false\n"
    )
    assert server.objects == {}


async def test_cli_skips_the_media_step_without_a_public_url(
    server: S3State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBJECT_STORAGE_MEDIA_BUCKET", MEDIA_BUCKET)
    monkeypatch.setenv("MEDIA_PUBLIC_BASE_URL", "")
    output = io.StringIO()

    async def sleep(seconds: float) -> None:
        await _link_runs_out(server, seconds)

    code = await cli.run([STORAGE_CHECK], output=output, sleep=sleep)

    assert code == 0
    assert output.getvalue().startswith("storage-check: media=skipped reason=no_public_url\n")


async def test_cli_with_a_public_url_but_no_media_bucket_fails(
    server: S3State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDIA_PUBLIC_BASE_URL", f"{server.endpoint}/{MEDIA_BUCKET}")
    output = io.StringIO()

    async def sleep(seconds: float) -> None:
        await _link_runs_out(server, seconds)

    code = await cli.run([STORAGE_CHECK], output=output, sleep=sleep)

    assert code == 1
    assert output.getvalue() == (
        "storage-check: result=failed step=media_put reason=not_configured probe_left=false\n"
    )
