"""The container probe (ticket #216): it must carry the Host that `TrustedHostMiddleware`
admits, or a production `public_base_url` turns every probe into a 400."""

from urllib.error import URLError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from ai_trainer.main import create_app
from scripts import healthcheck
from scripts.healthcheck import probe_request
from tests.unit.test_main import _settings


@pytest.mark.parametrize(
    "public_base_url",
    ["https://trainer.example", "http://localhost:8000", "https://t.example:8443"],
)
def test_the_probe_is_admitted_by_the_trusted_host_check(public_base_url: str) -> None:
    app = create_app(_settings(public_base_url=public_base_url))
    request = probe_request(public_base_url)

    response = TestClient(app).get("/health", headers={"Host": request.headers["Host"]})

    assert response.status_code != 400


def test_the_probe_targets_the_local_port_not_the_public_url() -> None:
    request = probe_request("https://trainer.example")

    assert request.full_url == "http://localhost:8000/health"


def test_main_exits_zero_when_the_app_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Request] = []
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://trainer.example")
    monkeypatch.setattr(healthcheck, "urlopen", lambda request, timeout: seen.append(request))

    assert healthcheck.main() == 0
    assert seen[0].headers["Host"] == "trainer.example"


def test_main_exits_one_when_the_app_does_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: Request, timeout: float) -> None:
        raise URLError("connection refused")

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://trainer.example")
    monkeypatch.setattr(healthcheck, "urlopen", refuse)

    assert healthcheck.main() == 1
