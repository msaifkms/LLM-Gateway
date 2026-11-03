"""Admin command line: manage users and API keys.

    python -m app.cli create-user alice --rate-limit 30
    python -m app.cli issue-key alice --label laptop
    python -m app.cli revoke-key 3
    python -m app.cli list-keys

In Docker:  docker compose run --rm gateway python -m app.cli list-keys
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import asyncpg

from app.auth import admin
from app.core.config import Settings
from app.db.pool import create_db_pool


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def positive_decimal(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description="LLM Gateway admin commands"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user", help="create a user")
    create.add_argument("name")
    create.add_argument(
        "--rate-limit", type=positive_int, default=60, help="requests per minute"
    )
    create.add_argument("--daily-tokens", type=positive_int, help="daily token cap")
    create.add_argument("--daily-usd", type=positive_decimal, help="daily cost cap")

    issue = commands.add_parser("issue-key", help="issue an API key for a user")
    issue.add_argument("user")
    issue.add_argument("--label", default="", help="a note, e.g. 'laptop'")
    issue.add_argument(
        "--no-cache", action="store_true", help="never cache this key's requests"
    )

    revoke = commands.add_parser("revoke-key", help="revoke an API key by id")
    revoke.add_argument("key_id", type=int)

    listing = commands.add_parser("list-keys", help="list API keys")
    listing.add_argument("user", nargs="?", help="only this user's keys")

    return parser


async def run_command(args: argparse.Namespace, pool: asyncpg.Pool) -> None:
    if args.command == "create-user":
        user_id = await admin.create_user(
            pool,
            args.name,
            rate_limit_per_min=args.rate_limit,
            daily_token_limit=args.daily_tokens,
            daily_usd_limit=args.daily_usd,
        )
        print(f"Created user {args.name!r} (id {user_id}).")

    elif args.command == "issue-key":
        issued = await admin.issue_key(
            pool, args.user, label=args.label, cache_enabled=not args.no_cache
        )
        print(f"Key id : {issued.key_id}")
        print(f"User   : {args.user}")
        print(f"Key    : {issued.plaintext}")
        print("Store this key now. It is shown only once and cannot be recovered.")

    elif args.command == "revoke-key":
        await admin.revoke_key(pool, args.key_id)
        print(f"Revoked key {args.key_id}.")

    elif args.command == "list-keys":
        rows = await admin.list_keys(pool, args.user)
        if not rows:
            print("No keys.")
        for row in rows:
            status = "revoked" if row["revoked_at"] else "active"
            cache = "cache" if row["cache_enabled"] else "no-cache"
            print(
                f"{row['id']:>4}  {row['user_name']:<16} {row['key_prefix']:<12} "
                f"{status:<8} {cache:<9} {row['label']}"
            )


async def run(args: argparse.Namespace, database_url: str) -> int:
    pool = await create_db_pool(database_url, max_size=2)
    try:
        await run_command(args, pool)
    except admin.AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        print("error: could not reach the database", file=sys.stderr)
        return 1
    finally:
        await pool.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if not settings.database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2
    return asyncio.run(run(args, settings.database_url))


if __name__ == "__main__":
    raise SystemExit(main())
