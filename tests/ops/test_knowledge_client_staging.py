"""`stage-pmc` and `stage-file` of the token client, and the `/stage-knowledge` skill that drives
them (ticket #464, ADR-0036): staging proposes sources only, file bytes go by presigned PUT from
disk, and neither the token nor the PUT link is ever printed."""

import json
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from scripts import knowledge_client
from tests.ops.test_knowledge_client import BASE_URL, TOKEN, _Response

SKILL = Path(__file__).parents[2] / ".claude" / "skills" / "stage-knowledge" / "SKILL.md"
PUT_URL = "https://r2.example/uploads/staging/abc.file?X-Amz-Signature=SECRET-SIG"
UPLOAD_ID = "11111111-1111-1111-1111-111111111111"
SOURCE_ID = "22222222-2222-2222-2222-222222222222"
PDF_BYTES = b"%PDF-1.7 not a model's business"
API = f"{BASE_URL}/api/admin/knowledge"


def _json(body: object) -> _Response:
    return _Response(json.dumps(body).encode())


def _article(pmcid: str) -> dict[str, object]:
    return {"pmcid": pmcid, "evidence_tier": "peer_reviewed", "sports": ["gym"], "topics": ["rm"]}


def _result(pmcid: str, outcome: str = "staged", **extra: object) -> dict[str, object]:
    return {"pmcid": pmcid, "outcome": outcome} | extra


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_API_URL", BASE_URL)
    monkeypatch.setenv("KNOWLEDGE_API_TOKEN", TOKEN)


@pytest.fixture
def seen() -> list[Request]:
    """Every request the client made; `_install` fills it."""
    return []


def _install(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    answer: Callable[[Request], Any],
) -> None:
    def fake(request: Request, timeout: float) -> Any:
        seen.append(request)
        result = answer(request)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(knowledge_client, "urlopen", fake)


def _manifest(tmp_path: Path, articles: list[dict[str, object]]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"articles": articles}))
    return path


def _template(tmp_path: Path) -> Path:
    path = tmp_path / "meta.json"
    path.write_text(json.dumps({"title": "Strength book", "licence": None}))
    return path


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "book.pdf"
    path.write_bytes(PDF_BYTES)
    return path


def _body(request: Request) -> dict[str, Any]:
    assert isinstance(request.data, bytes)
    parsed = json.loads(request.data)
    assert isinstance(parsed, dict)
    return parsed


def test_stage_pmc_posts_three_ids_and_reports_three_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = ["PMC1", "PMC2", "PMC3"]
    _install(
        monkeypatch,
        seen,
        lambda request: _json(
            {"results": [_result(i, source_id=SOURCE_ID, status="proposed") for i in ids]}
        ),
    )

    code = knowledge_client.main(
        ["stage-pmc", str(_manifest(tmp_path, [_article(i) for i in ids]))]
    )

    assert code == 0
    assert [a["pmcid"] for a in _body(seen[0])["articles"]] == ids
    assert seen[0].full_url == f"{API}/stage/pmc"
    assert seen[0].get_method() == "POST"
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    report = json.loads(capsys.readouterr().out)
    assert [(r["pmcid"], r["outcome"], r["status"]) for r in report["results"]] == [
        (i, "staged", "proposed") for i in ids
    ]


def test_stage_pmc_chunks_at_the_route_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = [f"PMC{n}" for n in range(1, 26)]
    _install(
        monkeypatch,
        seen,
        lambda request: _json(
            {"results": [_result(a["pmcid"]) for a in _body(request)["articles"]]}
        ),
    )

    code = knowledge_client.main(
        ["stage-pmc", str(_manifest(tmp_path, [_article(i) for i in ids]))]
    )

    assert code == 0
    assert [len(_body(r)["articles"]) for r in seen] == [20, 5]
    report = json.loads(capsys.readouterr().out)
    assert [r["pmcid"] for r in report["results"]] == ids


def test_stage_pmc_reports_refused_and_stages_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [
        _result("PMC1"),
        _result("PMC2", "refused", reason="licence_not_admitted", license_code="TDM"),
        _result("PMC3", "refused", reason="retracted"),
        _result("PMC4", "covered", status="ready"),
    ]
    _install(monkeypatch, seen, lambda request: _json({"results": results}))
    manifest = _manifest(tmp_path, [_article(r["pmcid"]) for r in results])  # type: ignore[arg-type]

    assert knowledge_client.main(["stage-pmc", str(manifest)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert [(r["pmcid"], r["outcome"], r.get("reason")) for r in report["results"]] == [
        ("PMC1", "staged", None),
        ("PMC2", "refused", "licence_not_admitted"),
        ("PMC3", "refused", "retracted"),
        ("PMC4", "covered", None),
    ]
    assert report["results"][1]["license_code"] == "TDM"


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"articles": []}', '{"articles": ["PMC1"]}', '{"articles": [{"x": 1}]}'],
    ids=["not-json", "not-object", "empty", "not-objects", "no-pmcid"],
)
def test_stage_pmc_refuses_a_bad_manifest_before_any_request(
    content: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, seen, lambda request: _json({}))
    path = tmp_path / "manifest.json"
    path.write_text(content)

    assert knowledge_client.main(["stage-pmc", str(path)]) == 1

    assert seen == []
    assert "manifest" in capsys.readouterr().err


def test_a_missing_input_file_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert knowledge_client.main(["stage-pmc", str(tmp_path / "nope.json")]) == 1
    assert "nope.json" in capsys.readouterr().err


def test_stage_pmc_a_422_names_what_the_route_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    detail = {"detail": "Name each PMC ID once."}
    error = HTTPError(f"{API}/stage/pmc", 422, "x", Message(), BytesIO(json.dumps(detail).encode()))
    _install(monkeypatch, seen, lambda request: error)

    assert knowledge_client.main(["stage-pmc", str(_manifest(tmp_path, [_article("PMC1")]))]) == 1

    err = capsys.readouterr().err
    assert "HTTP 422" in err
    assert "Name each PMC ID once." in err
    assert TOKEN not in err


def test_stage_file_puts_the_bytes_to_the_link_without_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def answer(request: Request) -> _Response:
        if request.full_url == f"{API}/stage/file":
            return _json(
                {
                    "outcome": "ready",
                    "upload_id": UPLOAD_ID,
                    "put_url": PUT_URL,
                    "expires_at": "2026-11-06T13:00:00Z",
                }
            )
        if request.full_url == PUT_URL:
            return _Response(b"")
        return _json(
            {"outcome": "registered", "source_id": SOURCE_ID, "status": "proposed",
             "file_type": "pdf", "size_bytes": len(PDF_BYTES)}
        )  # fmt: skip

    _install(monkeypatch, seen, answer)

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 0
    stage, put, register = seen
    assert _body(stage)["file_name"] == "book.pdf"
    assert _body(stage)["source"] == {"title": "Strength book", "licence": None}
    assert PDF_BYTES.decode() not in json.dumps(_body(stage))
    assert put.get_method() == "PUT"
    assert put.full_url == PUT_URL
    assert put.data == PDF_BYTES
    assert put.get_header("Authorization") is None
    assert register.full_url == f"{API}/register"
    assert _body(register) == {"upload_id": UPLOAD_ID}
    assert isinstance(register.data, bytes)
    assert PDF_BYTES not in register.data
    out = capsys.readouterr()
    assert json.loads(out.out) == {
        "outcome": "registered",
        "source_id": SOURCE_ID,
        "status": "proposed",
        "file_type": "pdf",
        "size_bytes": len(PDF_BYTES),
    }
    assert "SECRET-SIG" not in out.out + out.err


def test_stage_file_covered_issues_no_link_and_puts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    covered = {"outcome": "covered", "source_id": SOURCE_ID, "status": "ready"}
    _install(monkeypatch, seen, lambda request: _json(covered))

    assert knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))]) == 0

    assert len(seen) == 1
    assert json.loads(capsys.readouterr().out) == covered


def _ready_then(
    register_answers: Sequence[Mapping[str, object]], stages: list[str] | None = None
) -> Callable[[Request], _Response]:
    remaining = iter(register_answers)

    def answer(request: Request) -> _Response:
        if request.full_url == f"{API}/stage/file":
            if stages is not None:
                stages.append("stage")
            return _json({"outcome": "ready", "upload_id": UPLOAD_ID, "put_url": PUT_URL})
        if request.full_url == PUT_URL:
            return _Response(b"")
        return _json(next(remaining))

    return answer


@pytest.mark.parametrize("problem", ["too_large", "unlisted_type"])
def test_stage_file_reports_a_refused_object(
    problem: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, seen, _ready_then([{"outcome": "refused", "reason": problem}]))

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["outcome"], report["reason"]) == ("refused", problem)


def test_stage_file_puts_again_when_register_finds_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = [{"outcome": "missing"}, {"outcome": "registered", "source_id": SOURCE_ID}]
    _install(monkeypatch, seen, _ready_then(answers))

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 0
    assert [r.get_method() for r in seen] == ["POST", "PUT", "POST", "PUT", "POST"]
    assert json.loads(capsys.readouterr().out)["outcome"] == "registered"


def test_stage_file_stays_missing_after_one_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, seen, _ready_then([{"outcome": "missing"}, {"outcome": "missing"}]))

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "missing"
    assert len(seen) == 5


def test_stage_file_stages_again_once_when_the_link_expired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    stages: list[str] = []
    answers = [{"outcome": "expired"}, {"outcome": "registered", "source_id": SOURCE_ID}]
    _install(monkeypatch, seen, _ready_then(answers, stages))

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 0
    assert stages == ["stage", "stage"]


def test_stage_file_reports_expired_when_it_expires_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    stages: list[str] = []
    _install(
        monkeypatch, seen, _ready_then([{"outcome": "expired"}, {"outcome": "expired"}], stages)
    )

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 1
    assert stages == ["stage", "stage"]
    assert json.loads(capsys.readouterr().out)["outcome"] == "expired"


def test_stage_file_errors_never_print_the_put_url_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def answer(request: Request) -> object:
        if request.full_url == PUT_URL:
            return HTTPError(request.full_url, 403, "SignatureDoesNotMatch", Message(), BytesIO())
        return _json({"outcome": "ready", "upload_id": UPLOAD_ID, "put_url": PUT_URL})

    _install(monkeypatch, seen, answer)

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 1
    out = capsys.readouterr()
    assert "HTTP 403" in out.err
    for secret in ("SECRET-SIG", "r2.example", TOKEN):
        assert secret not in out.out + out.err


def test_stage_file_a_422_reports_the_field_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    detail = json.dumps({"detail": {"licence.attribution": "missing"}}).encode()
    error = HTTPError(f"{API}/stage/file", 422, "x", Message(), BytesIO(detail))
    _install(monkeypatch, seen, lambda request: error)

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 1
    assert "licence.attribution" in capsys.readouterr().err


def test_stage_file_a_missing_file_exits_1_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, seen, lambda request: _json({}))

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(tmp_path / "x.pdf")])

    assert code == 1
    assert seen == []
    assert "x.pdf" in capsys.readouterr().err


def test_staging_without_a_token_exits_3_before_reading_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: list[Request]
) -> None:
    monkeypatch.delenv("KNOWLEDGE_API_TOKEN")
    _install(monkeypatch, seen, lambda request: _json({}))

    assert knowledge_client.main(["stage-pmc", str(tmp_path / "m.json")]) == 3
    assert seen == []


def test_a_put_url_that_is_not_https_is_refused_and_not_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def answer(request: Request) -> _Response:
        return _json(
            {"outcome": "ready", "upload_id": UPLOAD_ID, "put_url": "http://evil.example/x"}
        )

    _install(monkeypatch, seen, answer)

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 1
    assert len(seen) == 1
    assert "evil.example" not in capsys.readouterr().err


def test_the_client_only_calls_list_stage_and_register_paths() -> None:
    source = Path(knowledge_client.__file__).read_text().lower()

    for forbidden in ("approve", "reingest", "re-ingest", "delete", "reject"):
        assert forbidden not in source


def test_skill_passes_a_path_never_file_contents() -> None:
    text = SKILL.read_text()

    assert "scripts/knowledge_client.py stage-file" in text
    assert "scripts/knowledge_client.py stage-pmc" in text
    assert "never" in text.lower()
    assert "presigned" in text.lower()
    assert "bytes" in text


def test_skill_lists_paywalled_sources_for_the_owner_instead_of_staging() -> None:
    text = SKILL.read_text()

    assert "paywall" in text.lower()
    assert "for the owner to obtain" in text.lower()


def test_skill_never_approves_deletes_or_reingests() -> None:
    text = SKILL.read_text()
    boundaries = text.split("## Boundaries", 1)[1]

    for word in ("approve", "delete", "re-ingest"):
        assert word in boundaries.lower()
    assert "proposed" in text
    assert "owner" in text
    assert "approved" in text


def test_skill_fills_templates_from_the_sources_own_metadata() -> None:
    text = SKILL.read_text()

    assert "memory" in text.lower()
    assert "none found" in text
    assert "licence" in text.lower()


@pytest.mark.parametrize("body", [b"not json", b'{"other": 1}'], ids=["not-json", "no-detail"])
def test_a_422_without_a_readable_detail_still_reports_the_status(
    body: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = HTTPError(f"{API}/stage/pmc", 422, "x", Message(), BytesIO(body))
    _install(monkeypatch, seen, lambda request: error)

    assert knowledge_client.main(["stage-pmc", str(_manifest(tmp_path, [_article("PMC1")]))]) == 1

    assert capsys.readouterr().err.strip().endswith("answered HTTP 422")


@pytest.mark.parametrize(
    "answer",
    [[], {"results": "no"}],
    ids=["not-an-object", "results-not-a-list"],
)
def test_stage_pmc_an_unexpected_answer_exits_1(
    answer: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, seen, lambda request: _json(answer))

    assert knowledge_client.main(["stage-pmc", str(_manifest(tmp_path, [_article("PMC1")]))]) == 1

    assert "unexpected shape" in capsys.readouterr().err


def test_stage_file_a_ready_answer_without_a_link_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, seen, lambda request: _json({"outcome": "ready"}))

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 1
    assert len(seen) == 1
    assert "unexpected shape" in capsys.readouterr().err


def test_stage_file_a_template_that_is_not_an_object_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: list[Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, seen, lambda request: _json({}))
    template = tmp_path / "meta.json"
    template.write_text("[]")

    assert knowledge_client.main(["stage-file", str(template), str(_pdf(tmp_path))]) == 1

    assert seen == []
    assert "template" in capsys.readouterr().err


def test_make_starts_storage_link_is_sent_to_loopback_with_the_signed_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: list[Request]
) -> None:
    dev_link = "http://storage:7070/uploads/staging/abc.file?X-Amz-Signature=SIG"
    monkeypatch.setenv("KNOWLEDGE_API_URL", "http://localhost:8000")

    def answer(request: Request) -> _Response:
        if request.full_url.endswith("/stage/file"):
            return _json({"outcome": "ready", "upload_id": UPLOAD_ID, "put_url": dev_link})
        if request.get_method() == "PUT":
            return _Response(b"")
        return _json({"outcome": "registered", "source_id": SOURCE_ID})

    _install(monkeypatch, seen, answer)

    code = knowledge_client.main(["stage-file", str(_template(tmp_path)), str(_pdf(tmp_path))])

    assert code == 0
    put = seen[1]
    assert put.full_url == "http://127.0.0.1:7070/uploads/staging/abc.file?X-Amz-Signature=SIG"
    assert put.get_header("Host") == "storage:7070"
    assert put.get_header("Authorization") is None


def test_a_storage_link_without_a_port_is_sent_to_loopback_without_one() -> None:
    url, host = knowledge_client.put_target("http://storage/x", "http://localhost:8000")

    assert (url, host) == ("http://127.0.0.1/x", "storage")


def test_the_dev_storage_host_is_refused_for_a_remote_app() -> None:
    with pytest.raises(knowledge_client.ClientError, match="must be https"):
        knowledge_client.put_target("http://storage:7070/x", BASE_URL)


@pytest.mark.parametrize(
    "argv",
    [["stage-pmc"], ["stage-file", "only-one"], ["list", "extra"]],
    ids=["pmc-no-path", "file-one-path", "list-extra"],
)
def test_the_wrong_number_of_paths_exits_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        knowledge_client.main(argv)

    assert excinfo.value.code == 2
