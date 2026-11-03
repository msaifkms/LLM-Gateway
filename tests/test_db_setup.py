import asyncio
import logging
import re
from pathlib import Path

import asyncpg
import pytest

from app.core.config import Settings
from app.db.migrate import MIGRATIONS_DIR, load_migrations
from app.db.pool import check_db

# --- settings -----------------------------------------------------------------


def test_database_settings_default_to_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_POOL_MAX", raising=False)

    settings = Settings.from_env()

    assert settings.database_url is None
    assert settings.db_pool_max == 10


def test_database_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", " postgresql://u:p@db:5432/gateway ")
    monkeypatch.setenv("DB_POOL_MAX", "4")

    settings = Settings.from_env()

    assert settings.database_url == "postgresql://u:p@db:5432/gateway"
    assert settings.db_pool_max == 4


@pytest.mark.parametrize("value", ["0", "-1"])
def test_pool_size_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DB_POOL_MAX", value)

    with pytest.raises(ValueError, match="DB_POOL_MAX"):
        Settings.from_env()


# --- migration files ----------------------------------------------------------


def test_shipped_migrations_load_in_order() -> None:
    migrations = load_migrations()

    assert migrations[0].version == "001_users_and_api_keys"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert all(m.sql.strip() for m in migrations)


def test_first_migration_defines_users_and_api_keys() -> None:
    sql = load_migrations(MIGRATIONS_DIR)[0].sql

    assert "CREATE TABLE users" in sql
    assert "CREATE TABLE api_keys" in sql
    assert re.search(r"key_hash\s+TEXT NOT NULL UNIQUE", sql)  # one hash, one key


def test_bad_migration_names_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "users.sql").write_text("SELECT 1;")

    with pytest.raises(ValueError, match="bad migration name"):
        load_migrations(tmp_path)


def test_duplicate_migration_numbers_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    (tmp_path / "001_second.sql").write_text("SELECT 2;")

    with pytest.raises(ValueError, match="duplicate migration number"):
        load_migrations(tmp_path)


def test_migrations_load_in_numeric_order(tmp_path: Path) -> None:
    (tmp_path / "002_later.sql").write_text("SELECT 2;")
    (tmp_path / "001_first.sql").write_text("SELECT 1;")

    assert [m.version for m in load_migrations(tmp_path)] == [
        "001_first",
        "002_later",
    ]


# --- check_db -----------------------------------------------------------------


class FakePool:
    """Stands in for the pool: check_db only calls fetchval()."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.queries: list[str] = []

    async def fetchval(self, query: str) -> int:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return 1


def test_check_db_is_true_when_postgres_answers() -> None:
    pool = FakePool()

    assert asyncio.run(check_db(pool)) is True  # type: ignore[arg-type]
    assert pool.queries == ["SELECT 1"]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("refused"),
        TimeoutError(),
        asyncpg.PostgresError("server problem"),
        asyncpg.InterfaceError("pool closed"),
    ],
)
def test_check_db_is_false_and_logs_when_postgres_fails(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(check_db(FakePool(error))) is False  # type: ignore[arg-type]

    assert "Postgres is unreachable" in caplog.text
