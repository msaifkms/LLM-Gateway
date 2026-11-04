from decimal import Decimal

import pytest

from app.cli import CLEAR, build_parser, main


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


def test_set_limits_changes_only_what_is_mentioned() -> None:
    args = parse("set-limits", "alice", "--rate-limit", "10")

    assert args.user == "alice"
    assert args.rate_limit == 10
    assert args.daily_tokens is None  # not mentioned: left alone
    assert args.daily_usd is None


def test_set_limits_accepts_numbers_or_none() -> None:
    args = parse(
        "set-limits", "alice", "--daily-tokens", "200000", "--daily-usd", "1.5"
    )

    assert args.daily_tokens == 200000
    assert args.daily_usd == Decimal("1.5")


def test_set_limits_none_means_remove_the_limit() -> None:
    args = parse("set-limits", "alice", "--daily-tokens", "none", "--daily-usd", "NONE")

    assert args.daily_tokens is CLEAR
    assert args.daily_usd is CLEAR


@pytest.mark.parametrize(
    "argv",
    [
        ["set-limits"],  # a user is required
        [
            "set-limits",
            "alice",
            "--rate-limit",
            "none",
        ],  # a rate limit cannot be removed
        ["set-limits", "alice", "--daily-tokens", "0"],
        ["set-limits", "alice", "--daily-usd", "-2"],
        ["kill-switch"],  # an action is required
        ["kill-switch", "maybe"],
    ],
)
def test_bad_set_limits_and_kill_switch_arguments_are_rejected(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)

    assert exc.value.code == 2


def test_kill_switch_actions() -> None:
    assert parse("kill-switch", "on", "--reason", "incident").reason == "incident"
    assert parse("kill-switch", "off").action == "off"
    assert parse("kill-switch", "status").reason == ""
