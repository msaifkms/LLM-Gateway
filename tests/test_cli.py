from decimal import Decimal

import pytest

from app.cli import build_parser, main


def parse(*argv: str):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(argv)


def test_create_user_defaults() -> None:
    args = parse("create-user", "alice")

    assert args.command == "create-user"
    assert args.name == "alice"
    assert args.rate_limit == 60
    assert args.daily_tokens is None
    assert args.daily_usd is None


def test_create_user_with_limits() -> None:
    args = parse(
        "create-user",
        "bob",
        "--rate-limit",
        "30",
        "--daily-tokens",
        "50000",
        "--daily-usd",
        "2.50",
    )

    assert args.rate_limit == 30
    assert args.daily_tokens == 50000
    assert args.daily_usd == Decimal("2.50")


def test_issue_key_options() -> None:
    args = parse("issue-key", "alice", "--label", "laptop", "--no-cache")

    assert (args.user, args.label, args.no_cache) == ("alice", "laptop", True)
    assert parse("issue-key", "alice").no_cache is False


def test_revoke_and_list() -> None:
    assert parse("revoke-key", "7").key_id == 7
    assert parse("list-keys").user is None
    assert parse("list-keys", "alice").user == "alice"


@pytest.mark.parametrize(
    "argv",
    [
        [],  # a command is required
        ["frobnicate"],
        ["create-user"],  # name is required
        ["create-user", "x", "--rate-limit", "0"],
        ["create-user", "x", "--rate-limit", "abc"],
        ["create-user", "x", "--daily-usd", "-1"],
        ["create-user", "x", "--daily-usd", "NaN"],
        ["revoke-key", "abc"],
    ],
)
def test_bad_arguments_are_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)

    assert exc.value.code == 2


def test_the_cli_refuses_to_run_without_a_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "")

    assert main(["list-keys"]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().err
