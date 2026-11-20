"""Hand a newly issued API key to AWS SSM Parameter Store instead of printing it.

In the cloud, admin commands run as one-off containers whose output goes to the
log service, so a printed key would end up in the logs. With
"issue-key NAME --to-ssm /llm-gateway/keys/NAME" the key is stored as an
encrypted SecureString and only the parameter name is printed. The admin reads it
once with "aws ssm get-parameter --with-decryption" and then deletes it.

boto3 is imported only when this is used, so the gateway itself never loads it.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient

#: SSM's own rules: letters, digits and _ . - /, fully qualified (leading /).
_ALLOWED = re.compile(r"^/[A-Za-z0-9_.\-/]+$")
_MAX_LENGTH = 1011
#: Prefixes AWS reserves for itself.
_RESERVED = ("/aws", "/ssm")


class KeyStorageError(Exception):
    """The key could not be stored. The message never contains the key."""


def validate_parameter_name(name: str) -> str:
    """Return the name if SSM would accept it, or raise ValueError saying why."""
    if not _ALLOWED.match(name):
        raise ValueError(
            "must start with '/' and use only letters, digits, '_', '.', '-' and '/'"
        )
    if len(name) > _MAX_LENGTH:
        raise ValueError(f"must be at most {_MAX_LENGTH} characters")
    if "//" in name or name.endswith("/"):
        raise ValueError("must not contain an empty path segment")
    if name.lower().startswith(_RESERVED):
        raise ValueError("must not start with /aws or /ssm (reserved by AWS)")
    return name


def put_secure_parameter(
    name: str,
    value: str,
    *,
    description: str,
    client: "SSMClient | None" = None,
) -> None:
    """Create a SecureString parameter. Never overwrites an existing one.

    Refusing to overwrite means a key nobody has read yet can never be replaced
    by another, and a name typo cannot clobber an unrelated parameter.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    if client is None:
        import boto3

        client = boto3.client("ssm")

    try:
        client.put_parameter(
            Name=name,
            Value=value,
            Type="SecureString",  # encrypted with the account's aws/ssm KMS key
            Tier="Standard",
            Overwrite=False,
            Description=description,
        )
    except client.exceptions.ParameterAlreadyExists:
        raise KeyStorageError(
            f"SSM parameter {name} already exists; read and delete it, "
            "or choose another name"
        ) from None
    except ClientError as exc:
        # Only the error code: AWS messages do not echo the value, but there is
        # no reason to risk it.
        code = exc.response.get("Error", {}).get("Code", "unknown error")
        raise KeyStorageError(f"could not store the key in SSM ({code})") from None
    except BotoCoreError as exc:  # no credentials, no region, network failure
        raise KeyStorageError(
            f"could not store the key in SSM ({type(exc).__name__})"
        ) from None
