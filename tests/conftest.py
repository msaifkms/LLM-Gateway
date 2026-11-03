from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def no_redis_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch a real Redis unless they opt in. Empty URL = disabled."""
    monkeypatch.setenv("REDIS_URL", "")


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client with the app lifespan running (shared HTTP client, router)."""
    with TestClient(app) as test_client:
        yield test_client
