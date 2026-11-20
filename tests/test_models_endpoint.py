import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.core.routes import DEFAULT_ROUTES
from app.main import app


def test_models_lists_every_alias_sorted(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == sorted(DEFAULT_ROUTES)
    assert all(m == {"id": m["id"], "object": "model"} for m in body["data"])


def test_models_does_not_reveal_providers_behind_an_alias(client: TestClient) -> None:
    text = client.get("/v1/models").text

    assert "anthropic-default" in text  # an alias of its own
    assert "targets" not in text


def test_models_needs_an_api_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(app.dependency_overrides, authenticate)

    assert client.get("/v1/models").status_code == 401
