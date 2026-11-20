"""issue-key --to-ssm: the key goes to SSM Parameter Store and is never printed."""

import asyncio
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from app import cli
from app.auth import admin, ssm
from app.auth.admin import IssuedKey

PLAINTEXT = "llmgw_THIS-IS-THE-SECRET-KEY-0123456789"
PARAM = "/llm-gateway/keys/alice"


def ssm_client() -> Any:
    """A real botocore client with fake credentials; Stubber stops any request."""
    return boto3.client(
        "ssm",
        region_name="us-east-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture
def stubbed() -> Iterator[tuple[Any, Stubber]]:
    client = ssm_client()
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


# --- parameter names --------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["/llm-gateway/keys/alice", "/llm-gateway/keys/ci_evals.v2", "/x"],
)
def test_good_parameter_names(name: str) -> None:
    assert ssm.validate_parameter_name(name) == name
    assert cli.build_parser().parse_args(["issue-key", "a", "--to-ssm", name]).to_ssm


@pytest.mark.parametrize(
    "name",
    [
        "llm-gateway/keys/alice",  # not fully qualified
        "/llm-gateway/keys/al ice",
        "/llm-gateway/keys/alice$",
        "/llm-gateway//alice",
        "/llm-gateway/keys/",
        "/aws/reference/x",
        "/SSM/x",
        "/" + "a" * 1011,
        "",
    ],
)
def test_bad_parameter_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        ssm.validate_parameter_name(name)
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["issue-key", "alice", "--to-ssm", name])
    assert exc.value.code == 2


def test_to_ssm_is_off_by_default() -> None:
    assert cli.build_parser().parse_args(["issue-key", "alice"]).to_ssm is None


# --- the SSM call -----------------------------------------------------------


def test_the_key_is_stored_as_a_secure_string_without_overwrite(
    stubbed: tuple[Any, Stubber],
) -> None:
    client, stubber = stubbed
    stubber.add_response(
        "put_parameter",
        {"Version": 1, "Tier": "Standard"},
        {
            "Name": PARAM,
            "Value": PLAINTEXT,
            "Type": "SecureString",
            "Tier": "Standard",
            "Overwrite": False,
            "Description": "key 7",
        },
    )

    ssm.put_secure_parameter(PARAM, PLAINTEXT, description="key 7", client=client)


def test_an_existing_parameter_is_never_overwritten(
    stubbed: tuple[Any, Stubber],
) -> None:
    client, stubber = stubbed
    stubber.add_client_error("put_parameter", "ParameterAlreadyExists")

    with pytest.raises(ssm.KeyStorageError, match="already exists") as exc:
        ssm.put_secure_parameter(PARAM, PLAINTEXT, description="d", client=client)
    assert PLAINTEXT not in str(exc.value)


def test_aws_errors_report_only_the_code(stubbed: tuple[Any, Stubber]) -> None:
    client, stubber = stubbed
    stubber.add_client_error(
        "put_parameter",
        "AccessDeniedException",
        service_message=f"denied, and the message echoes {PLAINTEXT}",
    )

    with pytest.raises(ssm.KeyStorageError) as exc:
        ssm.put_secure_parameter(PARAM, PLAINTEXT, description="d", client=client)
    assert "AccessDeniedException" in str(exc.value)
    assert PLAINTEXT not in str(exc.value)
    assert exc.value.__cause__ is None  # nothing chained that could carry it


# --- the CLI flow -----------------------------------------------------------


class Calls:
    def __init__(self) -> None:
        self.stored: list[tuple[str, str, str]] = []
        self.revoked: list[int] = []


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    record = Calls()

    async def fake_issue_key(
        pool: object, user: str, *, label: str, cache_enabled: bool
    ) -> IssuedKey:
        return IssuedKey(key_id=7, plaintext=PLAINTEXT)

    async def fake_revoke_key(pool: object, key_id: int) -> None:
        record.revoked.append(key_id)

    def fake_put(name: str, value: str, *, description: str) -> None:
        record.stored.append((name, value, description))

    monkeypatch.setattr(admin, "issue_key", fake_issue_key)
    monkeypatch.setattr(admin, "revoke_key", fake_revoke_key)
    monkeypatch.setattr(ssm, "put_secure_parameter", fake_put)
    return record


def run(*argv: str) -> None:
    args = cli.build_parser().parse_args(argv)
    asyncio.run(cli.run_command(args, pool=object()))


def test_to_ssm_stores_the_key_and_prints_only_where_it_is(
    calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    run("issue-key", "alice", "--to-ssm", PARAM)

    assert calls.stored == [(PARAM, PLAINTEXT, "LLM Gateway API key 7 for user alice")]
    assert calls.revoked == []
    out, err = capsys.readouterr()
    assert PLAINTEXT not in out + err
    assert "Key id    : 7" in out
    assert PARAM in out
    assert "get-parameter" in out and "--with-decryption" in out
    assert "delete-parameter" in out


def test_a_failed_store_revokes_the_key_and_never_shows_it(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing_put(name: str, value: str, *, description: str) -> None:
        raise ssm.KeyStorageError("could not store the key in SSM (AccessDenied)")

    monkeypatch.setattr(ssm, "put_secure_parameter", failing_put)

    with pytest.raises(admin.AdminError, match="Key 7 was revoked"):
        run("issue-key", "alice", "--to-ssm", PARAM)

    assert calls.revoked == [7]
    out, err = capsys.readouterr()
    assert PLAINTEXT not in out + err


def test_without_to_ssm_the_key_is_printed_as_before(
    calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    run("issue-key", "alice")

    assert calls.stored == []
    assert f"Key    : {PLAINTEXT}" in capsys.readouterr().out
