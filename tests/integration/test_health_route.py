from fastapi.testclient import TestClient

from ai_trainer.main import create_app
from ai_trainer.settings import Settings


def test_health_endpoint_returns_200_without_session() -> None:
    app = create_app(Settings())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"database": "ok"}
