"""The web UI: three static pages served at /ui, talking to the public API.

The pages hold no data and no secrets. Everything they show comes from the
same authenticated endpoints any client uses (/v1/chat/completions, /v1/usage,
/v1/models, /v1/evals/runs), with the API key the user pastes in. So the UI
adds no new way in: without a valid key it can show nothing.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UI_DIR = Path(__file__).parent
PAGES_DIR = UI_DIR / "pages"
STATIC_DIR = UI_DIR / "static"
PREFIX = "/ui"

#: Only same-origin files, no inline script or style, no plugins, no framing,
#: and fetch() may only reach this gateway. Trusted Types make any accidental
#: HTML injection sink (innerHTML and friends) throw instead of running.
CSP_DIRECTIVES = (
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "require-trusted-types-for 'script'",
    "trusted-types 'none'",
)
CONTENT_SECURITY_POLICY = "; ".join(CSP_DIRECTIVES)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    # Always revalidate, so a deploy is picked up at once.
    "Cache-Control": "no-cache",
}

PAGES = {
    "": "playground.html",
    "/usage": "usage.html",
    "/evals": "evals.html",
}

router = APIRouter(prefix=PREFIX, include_in_schema=False)


def _page(filename: str) -> Callable[[], Awaitable[FileResponse]]:
    path = PAGES_DIR / filename

    async def serve() -> FileResponse:
        return FileResponse(path, media_type="text/html; charset=utf-8")

    return serve


for _suffix, _filename in PAGES.items():
    router.add_api_route(_suffix or "", _page(_filename), methods=["GET"])


def is_ui_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class UISecurityHeadersMiddleware:
    """Add the headers above to every response under /ui (pages, files, 404s)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_ui_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.mount(f"{PREFIX}/static", StaticFiles(directory=STATIC_DIR), name="ui-static")
    app.add_middleware(UISecurityHeadersMiddleware)
