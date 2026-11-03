import asyncpg
import pytest
from conftest import TEST_PRINCIPAL
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.auth.keys import generate_api_key, hash_api_key
from app.auth.store import Principal
from app.main import app

BODY = {"model": "mock-1", "messages": [{"role": "user", "content": "Hello!"}]}


class FakeKeyStore:
    """Stands in for Postgres: maps key hashes to callers."""

    def __init__(
        self,
        principals: dict[str, Principal] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.principals = principals or {}
        self.error = error
        self.lookups: list[str] = []

    async def lookup(self, key_hash: str) -> Principal | None:
        self.lookups.append(key_hash)
        if self.error is not None:
            raise self.error
        return self.principals.get(key_hash)


@pytest.fixture
def key() -> str:
    return generate_api_key()


@pytest.fixture
def store(key: str, monkeypatch: pytest.MonkeyPatch) -> FakeKeyStore:
    """Turn real authentication back on, backed by a fake store with one key."""
    monkeypatch.delitem(app.dependency_overrides, authenticate)
    fake = FakeKeyStore({hash_api_key(key): TEST_PRINCIPAL})
    monkeypatch.setattr(app.state, "key_store", fake, raising=False)
    return fake


def post(
    client: TestClient, authorization: str | None = None
) -> tuple[int, dict[str, str], dict[str, object]]:
    headers = {"Authorization": authorization} if authorization else {}
    response = client.post("/v1/chat/completions", json=BODY, headers=headers)
    return response.status_code, dict(response.headers), response.json()


def test_a_valid_key_is_accepted(
    client: TestClient, store: FakeKeyStore, key: str
) -> None:
    status, headers, _ = post(client, f"Bearer {key}")

    assert status == 200
    assert headers["x-provider"] == "mock-1"
    assert store.lookups == [hash_api_key(key)]  # looked up by hash, never plaintext


def test_the_bearer_scheme_is_case_insensitive(
    client: TestClient, store: FakeKeyStore, key: str
) -> None:
    status, _, _ = post(client, f"bearer {key}")

    assert status == 200


def test_a_missing_header_is_401(client: TestClient, store: FakeKeyStore) -> None:
    status, headers, body = post(client)

    assert status == 401
    assert headers["www-authenticate"] == "Bearer"
    assert body == {"detail": "Invalid or missing API key"}
    assert store.lookups == []


@pytest.mark.parametrize(
    "authorization",
    ["Basic abc123", "Bearer", "Bearer ", "llmgw_nobearerword", "Bearer sk-not-ours"],
)
def test_malformed_credentials_are_401_without_a_database_lookup(
    client: TestClient, store: FakeKeyStore, authorization: str
) -> None:
    status, _, _ = post(client, authorization)

    assert status == 401
    assert store.lookups == []


def test_an_unknown_key_is_401_and_not_echoed_back(
    client: TestClient, store: FakeKeyStore
) -> None:
    stranger = generate_api_key()

    status, headers, body = post(client, f"Bearer {stranger}")

    assert status == 401
    assert stranger not in str(body)
    assert stranger not in str(headers)
    assert len(store.lookups) == 1


def test_unknown_and_malformed_keys_get_the_same_answer(
    client: TestClient, store: FakeKeyStore
) -> None:
    unknown = post(client, f"Bearer {generate_api_key()}")
    malformed = post(client, "Bearer nonsense")

    assert unknown[0] == malformed[0] == 401
    assert unknown[2] == malformed[2]  # callers cannot probe which keys exist


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("db down"),
        TimeoutError(),
        asyncpg.PostgresError("server problem"),
        asyncpg.InterfaceError("pool closed"),
    ],
)
def test_a_database_failure_refuses_the_request_instead_of_allowing_it(
    client: TestClient, store: FakeKeyStore, key: str, error: Exception
) -> None:
    store.error = error

    status, _, body = post(client, f"Bearer {key}")

    assert status == 503
    assert body == {"detail": "Authentication is unavailable"}


def test_without_a_database_nothing_can_authenticate(
    client: TestClient, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(app.dependency_overrides, authenticate)
    # the client fixture's startup left key_store as None (no DATABASE_URL)
    assert app.state.key_store is None

    status, _, _ = post(client, f"Bearer {key}")

    assert status == 503


def test_health_endpoints_need_no_key(client: TestClient, store: FakeKeyStore) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code in (200, 503)  # answers, never 401
