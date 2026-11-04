"""The token client for `/research-knowledge` (ticket #459, ADR-0036): it lists what production
already covers, reads its base URL and token from the environment, and never prints the token."""

import json
import runpy
import sys
from email.message import Message
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from types import TracebackType
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from scripts import knowledge_client

TOKEN = "tok-SECRET-0123456789abcdef"
BASE_URL = "https://trainer.example"
SKILL = Path(__file__).parents[2] / ".claude" / "skills" / "research-knowledge" / "SKILL.md"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _source(title: str, **identifiers: str | None) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "title": title,
        "status": "ready",
        "file_type": "pdf",
        "source_url": None,
        "pmcid": None,
        "doi": None,
        "pmid": None,
        "isbn": None,
        "license_id": "cc-by",
        "license_source": "admin",
        "license_url": None,
        "licence_flag": None,
        "content_hash": "abc",
    }
    return base | identifiers


def _page(sources: list[dict[str, object]], next_id: str | None = None) -> _Response:
    return _Response(json.dumps({"sources": sources, "next": next_id}).encode())


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_URL", BASE_URL)
    monkeypatch.setenv("KNOWLEDGE_API_TOKEN", TOKEN)


def test_list_returns_pmcid_doi_and_title_of_every_page(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pages = iter(
        [
            _page([_source("Volume review", pmcid="PMC1", doi="10.1/a")], next_id="cursor-2"),
            _page([_source("Frequency trial", pmcid=None, doi="10.1/b")]),
        ]
    )
    seen: list[Request] = []

    def fake(request: Request, timeout: float) -> _Response:
        seen.append(request)
        return next(pages)

    monkeypatch.setattr(knowledge_client, "urlopen", fake)

    assert knowledge_client.main(["list"]) == 0

    covered = json.loads(capsys.readouterr().out)
    assert [(s["title"], s["pmcid"], s["doi"]) for s in covered] == [
        ("Volume review", "PMC1", "10.1/a"),
        ("Frequency trial", None, "10.1/b"),
    ]
    assert "after=cursor-2" not in seen[0].full_url
    assert "after=cursor-2" in seen[1].full_url
    assert seen[0].full_url.startswith(f"{BASE_URL}/api/admin/knowledge/sources?")


def test_the_token_goes_in_the_header_not_the_url(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Request] = []

    def fake(request: Request, timeout: float) -> _Response:
        seen.append(request)
        return _page([])

    monkeypatch.setattr(knowledge_client, "urlopen", fake)

    assert knowledge_client.main(["list"]) == 0

    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in seen[0].full_url


def test_no_token_exits_3_and_says_so_without_a_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_URL", BASE_URL)
    monkeypatch.delenv("KNOWLEDGE_API_TOKEN", raising=False)
    calls: list[Request] = []
    monkeypatch.setattr(knowledge_client, "urlopen", lambda request, timeout: calls.append(request))

    assert knowledge_client.main(["list"]) == 3

    assert "KNOWLEDGE_API_TOKEN is not set" in capsys.readouterr().err
    assert calls == []


def test_an_empty_token_counts_as_not_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_URL", BASE_URL)
    monkeypatch.setenv("KNOWLEDGE_API_TOKEN", "  ")

    assert knowledge_client.main(["list"]) == 3
    assert "KNOWLEDGE_API_TOKEN is not set" in capsys.readouterr().err


def test_missing_base_url_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KNOWLEDGE_API_URL", raising=False)
    monkeypatch.setenv("KNOWLEDGE_API_TOKEN", TOKEN)

    assert knowledge_client.main(["list"]) == 1

    captured = capsys.readouterr()
    assert "KNOWLEDGE_API_URL is not set" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_http_error_never_prints_the_token(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(request: Request, timeout: float) -> _Response:
        raise HTTPError(request.full_url, 401, f"Unauthorized {TOKEN}", Message(), BytesIO())

    monkeypatch.setattr(knowledge_client, "urlopen", fake)

    assert knowledge_client.main(["list"]) == 1

    captured = capsys.readouterr()
    assert "401" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_connection_error_never_prints_the_token(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(request: Request, timeout: float) -> _Response:
        raise URLError(f"refused for {TOKEN}")

    monkeypatch.setattr(knowledge_client, "urlopen", fake)

    assert knowledge_client.main(["list"]) == 1

    captured = capsys.readouterr()
    assert captured.err.strip()
    assert TOKEN not in captured.out + captured.err


def test_a_dropped_connection_mid_response_never_prints_the_token(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(request: Request, timeout: float) -> _Response:
        raise IncompleteRead(TOKEN.encode())

    monkeypatch.setattr(knowledge_client, "urlopen", fake)

    assert knowledge_client.main(["list"]) == 1

    captured = capsys.readouterr()
    assert "IncompleteRead" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_bad_json_never_prints_the_token(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        knowledge_client,
        "urlopen",
        lambda request, timeout: _Response(f"<html>{TOKEN}</html>".encode()),
    )

    assert knowledge_client.main(["list"]) == 1

    captured = capsys.readouterr()
    assert captured.err.strip()
    assert TOKEN not in captured.out + captured.err


def test_an_unexpected_response_shape_exits_1(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        knowledge_client, "urlopen", lambda request, timeout: _Response(b'{"sources": 3}')
    )

    assert knowledge_client.main(["list"]) == 1
    assert "unexpected shape" in capsys.readouterr().err


def test_running_the_file_exits_with_the_no_token_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KNOWLEDGE_API_TOKEN", raising=False)
    monkeypatch.delitem(sys.modules, "scripts.knowledge_client")
    monkeypatch.setattr(sys, "argv", ["knowledge_client.py", "list"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("scripts.knowledge_client", run_name="__main__")

    assert excinfo.value.code == 3


@pytest.mark.parametrize(
    "url", ["http://trainer.example", "ftp://trainer.example", "trainer.example"]
)
def test_a_clear_text_or_odd_base_url_is_refused_before_any_request(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_URL", url)
    monkeypatch.setenv("KNOWLEDGE_API_TOKEN", TOKEN)
    calls: list[Request] = []
    monkeypatch.setattr(knowledge_client, "urlopen", lambda request, timeout: calls.append(request))

    assert knowledge_client.main(["list"]) == 1

    captured = capsys.readouterr()
    assert "must be an https URL" in captured.err
    assert TOKEN not in captured.out + captured.err
    assert calls == []


def test_http_is_allowed_for_localhost(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_URL", "http://localhost:8000")
    monkeypatch.setenv("KNOWLEDGE_API_TOKEN", TOKEN)
    monkeypatch.setattr(knowledge_client, "urlopen", lambda request, timeout: _page([]))

    assert knowledge_client.main(["list"]) == 0


def test_a_redirect_is_not_followed_so_the_token_stays_with_its_host() -> None:
    handler = knowledge_client._NoRedirect()
    request = Request(f"{BASE_URL}/x", headers={"Authorization": f"Bearer {TOKEN}"})

    assert (
        handler.redirect_request(request, None, 301, "Moved", {}, "https://elsewhere.example/")
        is None
    )


def test_urlopen_goes_through_the_opener_that_follows_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Opener:
        def open(self, request: Request, timeout: float) -> str:
            return f"opened {timeout}"

    monkeypatch.setattr(knowledge_client, "_OPENER", Opener())

    assert knowledge_client.urlopen(Request(BASE_URL), timeout=3) == "opened 3"


def test_a_repeated_cursor_stops_the_listing(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        knowledge_client,
        "urlopen",
        lambda request, timeout: _page([_source("Again")], next_id="same"),
    )

    assert knowledge_client.main(["list"]) == 1
    assert "repeated a page cursor" in capsys.readouterr().err


def test_an_entry_that_is_not_an_object_exits_1(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        knowledge_client, "urlopen", lambda request, timeout: _Response(b'{"sources": ["x"]}')
    )

    assert knowledge_client.main(["list"]) == 1
    assert "unexpected shape" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "hint"),
    [(401, "refused or expired"), (403, "refused or expired"), (429, "rate limited")],
)
def test_the_status_message_tells_a_refused_token_from_a_rate_limit(
    status: int,
    hint: str,
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake(request: Request, timeout: float) -> _Response:
        raise HTTPError(request.full_url, status, "x", Message(), BytesIO())

    monkeypatch.setattr(knowledge_client, "urlopen", fake)

    assert knowledge_client.main(["list"]) == 1
    assert hint in capsys.readouterr().err


def test_an_unknown_command_exits_2(configured: None) -> None:
    with pytest.raises(SystemExit) as excinfo:
        knowledge_client.main(["delete"])

    assert excinfo.value.code == 2


def test_skill_report_format_carries_pmcid_doi_and_licence() -> None:
    text = SKILL.read_text()

    assert "europepmc" in text.lower()
    assert "resultType=core" in text
    candidates = text.split("**Candidates**", 1)[1].split("**Rejected**", 1)[0]
    for field in ("PMC ID", "DOI", "licence"):
        assert field in candidates
    assert "10 requests per second" in text
    assert "cc by" in text
    assert "cc-by" in text


def test_skill_names_covered_candidates_by_matched_identifier() -> None:
    text = SKILL.read_text()

    assert "scripts/knowledge_client.py list" in text
    drop_step = text.split("**Drop the covered.**", 1)[1].split("4. **Rank", 1)[0]
    for word in ("PMC ID", "DOI", "title"):
        assert word in drop_step
    for word in ("named", "matched"):
        assert word in drop_step


def test_skill_skips_the_check_when_the_token_is_missing() -> None:
    text = SKILL.read_text()

    assert "exits 3" in text
    assert "No token set: skipping the production covered list" in text
