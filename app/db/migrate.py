"""A tiny migration runner: numbered .sql files, applied once, in order.

    python -m app.db.migrate

Each file runs in its own transaction and is recorded in schema_migrations, so
running the command again does nothing. An advisory lock makes two instances
starting at the same moment safe: one applies, the other waits and then skips.
"""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.core.config import Settings
from app.db.pool import create_db_pool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+$")
ADVISORY_LOCK_ID = 7_243_001  # arbitrary constant shared by every gateway instance


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read migration files in order and reject bad names or duplicate numbers."""
    migrations: list[Migration] = []
    numbers: set[str] = set()

    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.match(path.stem)
        if match is None:
            raise ValueError(
                f"bad migration name {path.name!r}: expected 001_short_name.sql"
            )
        if match.group(1) in numbers:
            raise ValueError(f"duplicate migration number in {path.name!r}")
        numbers.add(match.group(1))
        migrations.append(Migration(path.stem, path.read_text()))

    return migrations


async def apply_migrations(
    pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply every migration not yet recorded. Returns the versions applied."""
    migrations = load_migrations(directory)
    applied_now: list[str] = []

    async with pool.acquire() as conn:
        # Lock first: even creating the bookkeeping table must not race.
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_ID)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            rows = await conn.fetch("SELECT version FROM schema_migrations")
            done = {row["version"] for row in rows}

            for migration in migrations:
                if migration.version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)",
                        migration.version,
                    )
                applied_now.append(migration.version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_ID)

    return applied_now


async def main() -> None:
    settings = Settings.from_env()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")

    pool = await create_db_pool(settings.database_url, max_size=2)
    try:
        applied = await apply_migrations(pool)
    finally:
        await pool.close()

    if applied:
        print("Applied migrations:", ", ".join(applied))
    else:
        print("Database is up to date.")


if __name__ == "__main__":
    asyncio.run(main())
