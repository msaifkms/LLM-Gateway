import pytest
from fastapi.testclient import TestClient

from app.main import app


class FakePool:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    async def fetchval(self, query: str) -> int:
        if not self.healthy:
            raise ConnectionRefusedError("down")
        return 1

    async def close(self) -> None:
        return None


def test_not_ready_without_a_database(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "postgres": "disabled",
        "redis": "disabled",
    }


def test_ready_when_postgres_answers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state, "db", FakePool(healthy=True))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["postgres"] == "ok"


def test_not_ready_when_postgres_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state, "db", FakePool(healthy=False))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["postgres"] == "unavailable"


def test_liveness_never_depends_on_postgres(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state, "db", FakePool(healthy=False))

    assert client.get("/healthz").status_code == 200
