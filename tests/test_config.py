from urllib.parse import unquote, urlsplit

import pytest

from app.core.config import Settings, database_url_from_env

DB_PARTS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLMODE")


@pytest.fixture(autouse=True)
def clean_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in DB_PARTS:
        monkeypatch.delenv(name, raising=False)


def set_parts(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("value", ["none", "None", "off", "disabled", " OFF ", ""])
def test_redis_can_be_turned_off_with_a_word(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("REDIS_URL", value)

    assert Settings.from_env().redis_url is None


def test_a_redis_url_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "rediss://cache.internal:6379/0")

    assert Settings.from_env().redis_url == "rediss://cache.internal:6379/0"


def test_no_database_settings_means_no_database() -> None:
    assert database_url_from_env() is None
    assert Settings.from_env().database_url is None


def test_database_url_wins_over_the_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@one:5432/x")
    set_parts(monkeypatch, DB_HOST="two", DB_USER="u", DB_NAME="n")

    assert database_url_from_env() == "postgresql://a:b@one:5432/x"


def test_url_is_built_from_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    set_parts(
        monkeypatch,
        DB_HOST="db.example.internal",
        DB_PORT="6543",
        DB_NAME="gateway",
        DB_USER="gateway_admin",
        DB_PASSWORD="s3cret",
        DB_SSLMODE="require",
    )

    assert database_url_from_env() == (
        "postgresql://gateway_admin:s3cret@db.example.internal:6543/gateway"
        "?sslmode=require"
    )


def test_special_characters_in_the_password_are_escaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "p@ss:w/rd#?%&=+ ~"
    set_parts(monkeypatch, DB_HOST="h", DB_NAME="n", DB_USER="u", DB_PASSWORD=password)

    url = database_url_from_env()
    assert url is not None
    parts = urlsplit(url)
    assert parts.hostname == "h"
    assert parts.port == 5432  # the default
    assert parts.path == "/n"
    assert unquote(parts.password or "") == password
    assert parts.query == ""


def test_password_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    set_parts(monkeypatch, DB_HOST="h", DB_NAME="n", DB_USER="u")

    assert database_url_from_env() == "postgresql://u@h:5432/n"


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ({"DB_HOST": "h", "DB_NAME": "n"}, "DB_USER and DB_NAME"),
        ({"DB_HOST": "h", "DB_USER": "u"}, "DB_USER and DB_NAME"),
        ({"DB_HOST": "h", "DB_USER": "u", "DB_NAME": "n", "DB_PORT": "x"}, "DB_PORT"),
    ],
)
def test_incomplete_parts_are_rejected(
    monkeypatch: pytest.MonkeyPatch, parts: dict[str, str], message: str
) -> None:
    set_parts(monkeypatch, **parts)

    with pytest.raises(ValueError, match=message):
        database_url_from_env()
