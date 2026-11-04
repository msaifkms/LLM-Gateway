"""API key generation and hashing.

Keys are long random strings, so a plain SHA-256 is enough to store them safely:
unlike human passwords there is nothing to guess, which is why no salt or slow
hash (bcrypt) is needed. Only the hash is stored; the plaintext key is shown
once, when it is created.
"""

import hashlib
import secrets

KEY_PREFIX = "llmgw_"
#: Characters kept for display: the prefix plus 4 random characters.
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 4
#: Real keys are about 49 characters; anything far longer is junk, so reject it
#: before hashing or touching the database.
MAX_KEY_LENGTH = 128


def generate_api_key() -> str:
    """A new key with 256 bits of randomness, e.g. llmgw_Xq3...."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """The value stored in (and looked up from) the api_keys table."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def display_prefix(key: str) -> str:
    """Safe-to-show start of a key, so an admin can tell keys apart."""
    return key[:DISPLAY_PREFIX_LENGTH]


def looks_like_api_key(candidate: str) -> bool:
    """Cheap shape check done before any database lookup."""
    return (
        candidate.startswith(KEY_PREFIX)
        and len(candidate) > len(KEY_PREFIX)
        and len(candidate) <= MAX_KEY_LENGTH
    )
