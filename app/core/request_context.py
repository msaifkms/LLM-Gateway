"""Middleware: request IDs and one summary log line per request."""

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logs import request_id_var

log = logging.getLogger("app.request")

_VALID_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_QUIET_PATHS = {"/healthz", "/readyz"}


def _request_id_from(scope: Scope) -> str:
    incoming = Headers(scope=scope).get("x-request-id")
    if incoming and _VALID_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Pure ASGI middleware, so context variables reach the endpoint code."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from(scope)
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status = 500
        cache: str | None = None
        provider: str | None = None

        async def send_with_id(message: Message) -> None:
            nonlocal status, cache, provider
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                cache = headers.get("x-cache")
                provider = headers.get("x-provider")
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception("unhandled error")
            raise
        finally:
            path = scope["path"]
            if path in _QUIET_PATHS:
                level = logging.DEBUG
            elif status >= 500:
                level = logging.ERROR
            else:
                level = logging.INFO
            fields: dict[str, object] = {
                "method": scope["method"],
                "path": path,
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            }
            if cache:
                fields["cache"] = cache
            if provider:
                fields["provider"] = provider
            log.log(level, "request", extra={"fields": fields})
            request_id_var.reset(token)
