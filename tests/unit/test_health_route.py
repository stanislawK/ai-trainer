from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.web.health import build_health_router


class FakeDatabaseHealth:
    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable

    async def ping(self) -> bool:
        return self._reachable


def _client(*, reachable: bool) -> TestClient:
    app = FastAPI()
    app.include_router(build_health_router(FakeDatabaseHealth(reachable=reachable)))
    return TestClient(app)


def test_health_does_not_require_a_session() -> None:
    client = _client(reachable=True)

    response = client.get("/health")

    assert response.status_code == 200


def test_health_reports_ok_when_database_is_reachable() -> None:
    client = _client(reachable=True)

    response = client.get("/health")

    assert response.json() == {"database": "ok"}


def test_health_reports_unavailable_when_database_is_unreachable() -> None:
    client = _client(reachable=False)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"database": "unavailable"}
