"""The web UI at /ui: served with strict headers, and adding no way around auth."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate
from app.main import app
from app.ui import CONTENT_SECURITY_POLICY, PAGES, PAGES_DIR, STATIC_DIR

PAGE_PATHS = [f"/ui{suffix}" for suffix in PAGES]
STATIC_FILES = sorted(p for p in STATIC_DIR.rglob("*") if p.is_file())
JS_FILES = [p for p in STATIC_FILES if p.suffix == ".js"]


@pytest.fixture
def anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real authentication, so nothing below is let through by the test setup."""
    monkeypatch.delitem(app.dependency_overrides, authenticate)


# --- serving ------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_each_page_is_served_without_a_key(
    client: TestClient, anonymous: None, path: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "<nav" in response.text
    for link in PAGE_PATHS:
        assert f'href="{link}"' in response.text  # the shared top nav


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_the_current_page_is_marked_in_the_nav(client: TestClient, path: str) -> None:
    html = client.get(path).text

    assert html.count('aria-current="page"') == 1
    assert f'<a href="{path}" aria-current="page">' in html


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/ui/static/css/app.css", "text/css"),
        ("/ui/static/js/common.js", "javascript"),
        ("/ui/static/favicon.svg", "image/svg+xml"),
    ],
)
def test_static_files_are_served(client: TestClient, path: str, kind: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert kind in response.headers["content-type"]


def test_every_file_a_page_references_exists(client: TestClient) -> None:
    for page in PAGE_PATHS:
        html = client.get(page).text
        for ref in re.findall(r'(?:src|href)="(/ui/static/[^"]+)"', html):
            assert client.get(ref).status_code == 200, ref


def test_every_module_import_exists() -> None:
    for js in JS_FILES:
        for name in re.findall(r'from "\./([^"]+)"', js.read_text()):
            assert (js.parent / name).is_file(), f"{js.name} imports {name}"


@pytest.mark.parametrize(
    "path",
    [
        "/ui/static/../__init__.py",
        "/ui/static/%2e%2e/__init__.py",
        "/ui/static/..%2f..%2fmain.py",
        "/ui/static/nope.js",
        "/ui/pages/playground.html",
        "/ui/admin",
    ],
)
def test_nothing_else_is_reachable(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


# --- headers ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [*PAGE_PATHS, "/ui/static/js/common.js", "/ui/static/nope.js"]
)
def test_ui_responses_carry_strict_security_headers(
    client: TestClient, path: str
) -> None:
    headers = client.get(path).headers

    assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cross-origin-opener-policy"] == "same-origin"


def test_the_policy_allows_only_this_origin() -> None:
    directives = dict(
        d.split(" ", 1) for d in CONTENT_SECURITY_POLICY.split("; ") if " " in d
    )

    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"  # no inline, no eval, no CDN
    assert directives["style-src"] == "'self'"
    assert directives["connect-src"] == "'self'"  # the key can only go here
    assert directives["frame-ancestors"] == "'none'"
    assert directives["form-action"] == "'none'"
    assert "unsafe" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY


def test_api_responses_are_left_alone(client: TestClient) -> None:
    for path in ["/healthz", "/v1/models", "/uix"]:
        assert "content-security-policy" not in client.get(path).headers


# --- auth is not weakened -------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/v1/usage", "/v1/models", "/v1/evals/runs", "/v1/evals/runs/1"]
)
def test_the_apis_the_ui_calls_still_need_a_key(
    client: TestClient, anonymous: None, path: str
) -> None:
    assert client.get(path).status_code == 401
    assert (
        client.get(path, headers={"Referer": "http://testserver/ui"}).status_code == 401
    )


def test_a_ui_page_does_not_authenticate_anything(
    client: TestClient, anonymous: None
) -> None:
    page = client.get("/ui")

    assert "set-cookie" not in page.headers  # no session the API could trust
    assert client.get("/v1/usage").status_code == 401


def test_the_ui_offers_no_admin_actions() -> None:
    text = " ".join(p.read_text() for p in [*PAGES_DIR.iterdir(), *JS_FILES]).lower()

    for word in ["issue-key", "revoke-key", "create-user", "kill-switch", "set-limits"]:
        assert word not in text
    # The only call that changes anything is a chat request.
    calls = re.findall(
        r'api\(\s*[`"]([^`"]+)[`"]\s*(,\s*\{[^)]*?method:\s*"(\w+)")?', text
    )
    assert calls
    assert {(path, method) for path, _, method in calls if method} == {
        ("/v1/chat/completions", "post")
    }


# --- what the pages are allowed to contain ------------------------------------


class Markup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.inline_script = False
        self.styles = 0
        self.style_attrs = 0
        self.handlers: list[str] = []
        self.controls: list[dict[str, str | None]] = []
        self.label_for: set[str] = set()
        self.urls: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script":
            self.scripts.append(a)
            self._in_script = True
        if tag == "style":
            self.styles += 1
        if "style" in a:
            self.style_attrs += 1
        self.handlers += [name for name in a if name.startswith("on")]
        if tag in {"input", "select", "textarea"}:
            self.controls.append(a)
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"] or "")
        self.urls += [v for k, v in a.items() if k in {"src", "href"} and v]

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_script = True


@pytest.mark.parametrize("page", sorted(PAGES_DIR.glob("*.html")))
def test_pages_have_no_inline_code_and_no_third_party_assets(page: Path) -> None:
    markup = Markup()
    markup.feed(page.read_text())

    assert markup.scripts and all(s.get("src") for s in markup.scripts)
    assert not markup.inline_script
    assert markup.styles == 0
    assert markup.style_attrs == 0
    assert markup.handlers == []
    for url in markup.urls:
        assert url.startswith(("/ui", "#")), url


@pytest.mark.parametrize("page", sorted(PAGES_DIR.glob("*.html")))
def test_every_form_control_has_a_label(page: Path) -> None:
    markup = Markup()
    markup.feed(page.read_text())

    for control in markup.controls:
        assert control.get("id") in markup.label_for, control


@pytest.mark.parametrize("js", JS_FILES, ids=lambda p: p.name)
def test_scripts_never_parse_data_as_html_or_code(js: Path) -> None:
    source = js.read_text()

    for sink in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"]:
        assert sink not in source
    assert not re.search(r"\beval\(|new Function\(", source)
    assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", source)


def test_the_key_lives_only_in_session_storage() -> None:
    source = (STATIC_DIR / "js" / "common.js").read_text()

    assert "sessionStorage.setItem(KEY_STORAGE, key)" in source
    assert "localStorage.setItem(KEY_STORAGE" not in source
    assert "document.cookie" not in source
    # The key goes out only as a header on same-origin /v1/ calls.
    assert 'if (!path.startsWith("/v1/"))' in source
    assert "Authorization: `Bearer ${key}`" in source
    assert 'credentials: "omit"' in source
    assert 'redirect: "error"' in source
    for js in JS_FILES:
        text = js.read_text()
        assert "console.log" not in text
        if js.name != "common.js":
            assert "sessionStorage" not in text and "fetch(" not in text
