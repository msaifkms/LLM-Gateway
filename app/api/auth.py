"""Authentication: turn an Authorization header into the calling user.

Fails closed. If the key cannot be checked (no database, database down) the
request is refused with 503 instead of being let through.
"""

import logging
from typing import Annotated

import asyncpg
from fastapi import Header, HTTPException, Request

from app.auth.keys import hash_api_key, looks_like_api_key
from app.auth.store import KeyStore, Principal

logger = logging.getLogger(__name__)

UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def bearer_token(authorization: str | None) -> str | None:
    """The token from 'Authorization: Bearer <token>', or None if malformed."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def invalid_key() -> HTTPException:
    # One message for every failure, so callers cannot tell "wrong key" from
    # "revoked key" and probe which keys exist.
    return HTTPException(
        status_code=401,
        detail="Invalid or missing API key",
        headers=UNAUTHORIZED_HEADERS,
    )


async def authenticate(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    token = bearer_token(authorization)
    if token is None or not looks_like_api_key(token):
        raise invalid_key()  # no database lookup for junk

    store: KeyStore | None = request.app.state.key_store
    if store is None:
        raise HTTPException(status_code=503, detail="Authentication is unavailable")

    try:
        principal = await store.lookup(hash_api_key(token))
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
        logger.warning("API key lookup failed; refusing the request")
        raise HTTPException(
            status_code=503, detail="Authentication is unavailable"
        ) from None

    if principal is None:
        raise invalid_key()
    return principal
