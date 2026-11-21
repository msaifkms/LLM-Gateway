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


# --- the visual theme ----------------------------------------------------------


def test_every_icon_used_is_in_each_pages_sprite() -> None:
    js_icons: set[str] = set()
    for js in JS_FILES:
        source = js.read_text()
        js_icons |= set(re.findall(r'icon\("([a-z-]+)"', source))
        # stat(...) calls pass an icon name: the lowercase literals on those
        # lines (labels and values are capitalised or contain spaces).
        for line in source.splitlines():
            if "stat(" in line and "function stat" not in line:
                js_icons |= set(re.findall(r'"([a-z][a-z-]*)"', line))
    assert js_icons, "the scripts use sprite icons"
    for page in sorted(PAGES_DIR.glob("*.html")):
        html = page.read_text()
        defined = set(re.findall(r'<symbol id="i-([a-z-]+)"', html))
        used = set(re.findall(r'<use href="#i-([a-z-]+)"', html))
        assert used <= defined, (page.name, used - defined)
        assert js_icons <= defined, (page.name, js_icons - defined)


def test_the_playground_keeps_every_element_its_script_uses() -> None:
    html = (PAGES_DIR / "playground.html").read_text()
    source = (STATIC_DIR / "js" / "playground.js").read_text()

    for element_id in set(re.findall(r'\$\("#([a-z-]+)"\)', source)):
        assert f'id="{element_id}"' in html, element_id


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _parse(color: str, under: tuple[int, int, int]) -> tuple[int, int, int]:
    """#rrggbb, or rgb(r g b / a) composited over the colour underneath."""
    color = color.strip()
    if color.startswith("#"):
        return _hex(color)
    match = re.fullmatch(r"rgb\((\d+) (\d+) (\d+) / ([\d.]+)\)", color)
    assert match, color
    r, g, b, a = (float(x) for x in match.groups())
    return tuple(
        round(a * c + (1 - a) * u) for c, u in zip((r, g, b), under, strict=True)
    )  # type: ignore[return-value]


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", block))


def _themes() -> dict[str, dict[str, str]]:
    css = (STATIC_DIR / "css" / "app.css").read_text()
    light = _tokens(
        css[css.index(":root {") : css.index("@media (prefers-color-scheme: dark)")]
    )
    dark_start = css.index(':root[data-theme="dark"] {')
    dark = light | _tokens(css[dark_start : css.index("}", dark_start)])
    return {"light": light, "dark": dark}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_colours_meet_wcag_aa(theme: str) -> None:
    t = _themes()[theme]
    surface = _hex(t["surface"])
    bg = _hex(t["bg"])

    def check(fg: str, under: tuple[int, int, int], need: float) -> None:
        ratio = _contrast(_parse(t[fg], under), under)
        assert ratio >= need, f"{theme}: --{fg} is {ratio:.2f}:1, needs {need}"

    for fg in ("text", "text-2", "accent"):
        check(fg, surface, 4.5)
        check(fg, bg, 4.5)
    check("text-2", _hex(t["surface-2"]), 4.5)
    check("control-border", surface, 3)  # form-control boundaries
    check("focus", bg, 3)
    check("focus", surface, 3)
    for kind in ("hit", "miss", "bypass", "coalesced", "pass", "fail", "warn"):
        badge = _parse(t[f"{kind}-bg"], surface)
        ratio = _contrast(_parse(t[f"{kind}-fg"], badge), badge)
        assert ratio >= 4.5, f"{theme}: {kind} badge is {ratio:.2f}:1"
    # Text on the gradient: every stop must carry the dark text at AA.
    for stop in re.findall(r"#[0-9a-f]{6}", t["gradient"]):
        ratio = _contrast(_hex(t["on-gradient"]), _hex(stop))
        assert ratio >= 4.5, f"{theme}: text on gradient stop {stop} is {ratio:.2f}:1"


def test_motion_respects_reduced_motion() -> None:
    css = (STATIC_DIR / "css" / "app.css").read_text()

    assert "@media (prefers-reduced-motion: reduce)" in css
    # Animations are only switched on when motion is welcome.
    assert "@media (prefers-reduced-motion: no-preference)" in css


def test_the_hidden_attribute_always_hides() -> None:
    """Buttons set display: inline-flex, which would otherwise beat [hidden]."""
    css = (STATIC_DIR / "css" / "app.css").read_text()

    assert "[hidden] { display: none !important; }" in css
