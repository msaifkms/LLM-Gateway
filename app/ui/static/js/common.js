// Shared by every page: the API key, calls to the gateway, the top bar, and
// small DOM and formatting helpers.
//
// The API key is kept in sessionStorage only: it lasts until the tab is
// closed, is never written to localStorage or a cookie, never put in a URL,
// and is sent only as an Authorization header on same-origin requests to /v1/.
// The Content-Security-Policy (connect-src 'self') backs that up: the browser
// refuses to send any request to another origin.

const KEY_STORAGE = "llmgw.apiKey";
const THEME_STORAGE = "llmgw.theme";
const KEY_PATTERN = /^llmgw_[A-Za-z0-9_-]{16,}$/;

// --- the API key --------------------------------------------------------------

export function getKey() {
  try {
    return sessionStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

function setKey(key) {
  sessionStorage.setItem(KEY_STORAGE, key);
  document.dispatchEvent(new CustomEvent("llmgw:key-changed"));
}

function clearKey() {
  try {
    sessionStorage.removeItem(KEY_STORAGE);
  } catch {
    /* nothing stored */
  }
  document.dispatchEvent(new CustomEvent("llmgw:key-changed"));
}

export function onKeyChange(callback) {
  document.addEventListener("llmgw:key-changed", callback);
}

// --- calling the gateway ------------------------------------------------------

export class ApiError extends Error {
  constructor(status, title, message, retryAfter = null) {
    super(message);
    this.status = status;
    this.title = title;
    this.retryAfter = retryAfter;
  }
}

function describeFailure(status, detail, retryAfter) {
  switch (status) {
    case 401:
      return ["API key rejected", "The key is missing, wrong or revoked. Paste a valid key above."];
    case 429:
      return [
        "Slow down",
        retryAfter !== null
          ? `${detail || "Rate limit or daily quota reached"}. Try again in ${retryAfter} s.`
          : detail || "Rate limit or daily quota reached.",
      ];
    case 503:
      return ["Service unavailable", `${detail || "The gateway cannot serve this right now"}. Try again shortly.`];
    case 400:
      return ["Request not accepted", detail || "The gateway rejected the request."];
    case 404:
      return ["Not found", detail || "Nothing here."];
    case 422:
      return ["Invalid input", "Check the form values and try again."];
    case 502:
    case 504:
      return ["Provider problem", detail || "The model provider did not answer properly."];
    default:
      return [`Error ${status}`, detail || "Something went wrong."];
  }
}

/**
 * Call a gateway endpoint under /v1/ with the stored key.
 * Returns { data, headers, ms }. Throws ApiError with a user-facing message.
 */
export async function api(path, { method = "GET", body = undefined, headers = {} } = {}) {
  if (!path.startsWith("/v1/")) {
    throw new Error("the UI only calls the gateway's own /v1/ API");
  }
  const key = getKey();
  if (!key) {
    throw new ApiError(0, "No API key", "Paste your API key above to continue.");
  }

  const init = {
    method,
    headers: { Authorization: `Bearer ${key}`, Accept: "application/json", ...headers },
    credentials: "omit",
    cache: "no-store",
    redirect: "error", // never carry the key along a redirect
    referrerPolicy: "no-referrer",
  };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  const started = performance.now();
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "Cannot reach the gateway", "Check your connection and try again.");
  }
  const ms = performance.now() - started;

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok) {
    const raw = response.headers.get("Retry-After");
    const retryAfter = raw !== null && /^\d+$/.test(raw) ? Number(raw) : null;
    const detail = data && typeof data.detail === "string" ? data.detail : "";
    const [title, message] = describeFailure(response.status, detail, retryAfter);
    if (response.status === 401) {
      openKeyPanel();
    }
    throw new ApiError(response.status, title, message, retryAfter);
  }
  return { data, headers: response.headers, ms };
}

// --- DOM helpers (text only: data is never parsed as HTML) --------------------

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function $(selector) {
  return document.querySelector(selector);
}

/** Show an ApiError (or any error) in an alert box. */
export function showError(box, error) {
  box.className = error && error.status === 429 ? "alert alert-warning" : "alert alert-error";
  box.replaceChildren(
    el("strong", { text: error.title || "Something went wrong" }),
    el("p", { text: error.message || String(error) }),
  );
  box.hidden = false;
}

export function hideAlert(box) {
  box.hidden = true;
  box.replaceChildren();
}

// --- formatting ---------------------------------------------------------------

const integer = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

export function fmtInt(n) {
  return integer.format(Number(n) || 0);
}

/** Dollars from the API's fixed-point strings. Small amounts keep their digits. */
export function fmtUsd(value) {
  const n = Number(value) || 0;
  if (n === 0) return "$0.00";
  if (Math.abs(n) < 0.01) return `$${n.toFixed(6)}`;
  return `$${n.toFixed(n < 100 ? 4 : 2)}`;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return "–";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export function fmtPct(rate) {
  if (rate === null || rate === undefined) return "–";
  return `${(rate * 100).toFixed(rate === 1 || rate === 0 ? 0 : 1)}%`;
}

export function fmtDateTime(iso) {
  if (!iso) return "–";
  const date = new Date(iso);
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// --- the top bar: key panel and theme toggle ----------------------------------

function openKeyPanel() {
  const panel = $("#key-panel");
  if (!panel) return;
  panel.hidden = false;
  $("#key-toggle")?.setAttribute("aria-expanded", "true");
  $("#api-key")?.focus();
}

function renderKeyStatus() {
  const key = getKey();
  const status = $("#key-status");
  const toggle = $("#key-toggle");
  if (status) {
    status.replaceChildren(
      key
        ? el("span", {}, "Using key ", el("code", { text: `${key.slice(0, 10)}…` }), " for this tab.")
        : el("span", { text: "No key set. Your key stays in this tab only (sessionStorage) and is sent only to this gateway." }),
    );
  }
  if (toggle) {
    toggle.textContent = key ? "Key ✓" : "Set key";
    toggle.setAttribute("aria-label", key ? "API key set: change or forget it" : "Set your API key");
  }
  const forget = $("#key-forget");
  if (forget) forget.hidden = !key;
}

function setupKeyPanel() {
  const panel = $("#key-panel");
  const toggle = $("#key-toggle");
  const form = $("#key-form");
  const input = $("#api-key");
  const error = $("#key-error");
  if (!panel || !toggle || !form || !input) return;

  toggle.addEventListener("click", () => {
    const open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const key = input.value.trim();
    if (!KEY_PATTERN.test(key)) {
      error.textContent = "That does not look like a gateway key (it starts with llmgw_).";
      error.hidden = false;
      input.focus();
      return;
    }
    error.hidden = true;
    input.value = "";
    setKey(key);
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.focus();
  });

  $("#key-forget")?.addEventListener("click", () => {
    clearKey();
    input.focus();
  });

  onKeyChange(renderKeyStatus);
  renderKeyStatus();
  if (!getKey()) openKeyPanel();
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "system";
}

function setupThemeToggle() {
  const button = $("#theme-toggle");
  if (!button) return;
  const order = ["system", "light", "dark"];
  const labels = { system: "◐ Auto", light: "☀ Light", dark: "☾ Dark" };
  const render = () => {
    const theme = currentTheme();
    button.textContent = labels[theme];
    button.setAttribute("aria-label", `Theme: ${theme === "system" ? "follow the system" : theme}. Click to change.`);
  };
  button.addEventListener("click", () => {
    const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
    if (next === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", next);
    try {
      if (next === "system") localStorage.removeItem(THEME_STORAGE);
      else localStorage.setItem(THEME_STORAGE, next);
    } catch {
      /* not remembered, still applied */
    }
    render();
  });
  render();
}

setupKeyPanel();
setupThemeToggle();
