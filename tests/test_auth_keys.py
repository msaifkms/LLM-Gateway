import re

import pytest

from app.auth.keys import (
    DISPLAY_PREFIX_LENGTH,
    KEY_PREFIX,
    MAX_KEY_LENGTH,
    display_prefix,
    generate_api_key,
    hash_api_key,
    looks_like_api_key,
)


def test_generated_keys_have_the_expected_shape() -> None:
    key = generate_api_key()

    assert key.startswith(KEY_PREFIX)
    assert re.fullmatch(r"llmgw_[A-Za-z0-9_-]{43}", key)  # 256 bits, URL-safe
    assert looks_like_api_key(key)


def test_generated_keys_are_unique() -> None:
    assert len({generate_api_key() for _ in range(1000)}) == 1000


def test_hash_is_deterministic_hex_and_differs_per_key() -> None:
    key = generate_api_key()

    assert hash_api_key(key) == hash_api_key(key)
    assert re.fullmatch(r"[0-9a-f]{64}", hash_api_key(key))
    assert hash_api_key(key) != hash_api_key(generate_api_key())


def test_the_hash_does_not_contain_the_key() -> None:
    key = generate_api_key()

    assert key not in hash_api_key(key)
    assert key.removeprefix(KEY_PREFIX) not in hash_api_key(key)


def test_display_prefix_reveals_only_the_start() -> None:
    key = generate_api_key()

    shown = display_prefix(key)

    assert shown == key[:DISPLAY_PREFIX_LENGTH]
    assert len(shown) == len(KEY_PREFIX) + 4
    assert len(shown) < len(key)


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "llmgw_",  # prefix only
        "sk-live-abcdef",  # wrong prefix
        "LLMGW_abc",  # prefix is case-sensitive
        KEY_PREFIX + "x" * MAX_KEY_LENGTH,  # absurdly long
    ],
)
def test_junk_does_not_look_like_a_key(candidate: str) -> None:
    assert looks_like_api_key(candidate) is False
