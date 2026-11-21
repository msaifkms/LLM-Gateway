import { $, api, ApiError, cacheBadge, el, fmtInt, fmtMs, fmtUsd, getKey, hideAlert, onKeyChange, showError } from "./common.js";

const form = $("#chat-form");
const model = $("#model");
const message = $("#message");
const temperature = $("#temperature");
const temperatureValue = $("#temperature-value");
const bypass = $("#bypass-cache");
const send = $("#send");
const sendStatus = $("#send-status");
const errorBox = $("#chat-error");
const reply = $("#reply");
const sent = $("#sent");
const copyButton = $("#copy-request-id");

const PREFERRED_MODEL = "mock-1"; // free and always available
let cooldownTimer = null;

function setDetail(id, value) {
  $(id).replaceChildren(value instanceof Node ? value : document.createTextNode(value));
}

function resetDetails() {
  for (const id of ["#d-provider", "#d-cache", "#d-latency", "#d-request-id", "#d-tokens", "#d-cost", "#d-model"]) {
    setDetail(id, "–");
  }
  copyButton.hidden = true;
}

/** Restart a one-off CSS animation (the cache HIT glow) on an element. */
function flash(element) {
  element.classList.remove("flash");
  void element.offsetWidth; // reflow, so re-adding the class replays it
  element.classList.add("flash");
}

/** Three bouncing dots while the gateway works (static with reduced motion). */
function showTyping() {
  reply.classList.add("placeholder");
  reply.replaceChildren(
    el("span", { class: "typing", "aria-hidden": "true" }, el("span"), el("span"), el("span")),
    el("span", { class: "visually-hidden", text: "Waiting for the gateway…" }),
  );
}

function showReply(text, placeholder = false) {
  reply.classList.toggle("placeholder", placeholder);
  reply.classList.remove("appear");
  void reply.offsetWidth;
  reply.classList.add("appear");
  reply.textContent = text;
}

copyButton.addEventListener("click", async () => {
  const id = $("#d-request-id").textContent;
  const label = copyButton.querySelector("span");
  try {
    await navigator.clipboard.writeText(id);
    label.textContent = "Copied";
  } catch {
    // No clipboard access (for example over plain HTTP): select the text instead.
    const range = document.createRange();
    range.selectNodeContents($("#d-request-id"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    label.textContent = "Selected";
  }
  setTimeout(() => {
    label.textContent = "Copy";
  }, 1500);
});

async function loadModels() {
  if (!getKey()) {
    model.replaceChildren(el("option", { value: "", text: "Set your API key to load models" }));
    model.disabled = true;
    return;
  }
  try {
    const { data } = await api("/v1/models");
    const ids = data.data.map((m) => m.id);
    model.replaceChildren(...ids.map((id) => el("option", { value: id, text: id })));
    model.value = ids.includes(PREFERRED_MODEL) ? PREFERRED_MODEL : ids[0];
    model.disabled = false;
    hideAlert(errorBox);
  } catch (error) {
    model.replaceChildren(el("option", { value: "", text: "Could not load models" }));
    model.disabled = true;
    showError(errorBox, error);
  }
}

function startCooldown(seconds) {
  clearInterval(cooldownTimer);
  let left = seconds;
  send.disabled = true;
  const tick = () => {
    if (left <= 0) {
      clearInterval(cooldownTimer);
      send.disabled = false;
      sendStatus.textContent = "";
      return;
    }
    sendStatus.textContent = `You can send again in ${left} s`;
    left -= 1;
  };
  tick();
  cooldownTimer = setInterval(tick, 1000);
}

temperature.addEventListener("input", () => {
  temperatureValue.textContent = Number(temperature.value).toFixed(1);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideAlert(errorBox);

  const text = message.value.trim();
  if (!text) {
    showError(errorBox, new ApiError(0, "Nothing to send", "Type a message first."));
    message.focus();
    return;
  }
  if (!model.value) {
    showError(errorBox, new ApiError(0, "No model", "Set your API key so the model list can load."));
    return;
  }

  send.disabled = true;
  sendStatus.textContent = "Sending…";
  sent.textContent = text;
  sent.hidden = false;
  showTyping();
  resetDetails();

  try {
    const { data, headers, ms } = await api("/v1/chat/completions", {
      method: "POST",
      body: {
        model: model.value,
        messages: [{ role: "user", content: text }],
        temperature: Number(temperature.value),
      },
      headers: bypass.checked ? { "Cache-Control": "no-store" } : {},
    });

    const choice = data.choices && data.choices[0];
    showReply(choice ? choice.message.content : "(no answer)");

    const cache = headers.get("X-Cache") || "–";
    // A cache hit was answered by no provider at all.
    setDetail("#d-provider", headers.get("X-Provider") || (cache === "HIT" ? "From cache" : "–"));
    setDetail("#d-cache", cacheBadge(cache));
    if (cache === "HIT") flash($("#card-cache"));
    setDetail("#d-latency", fmtMs(ms));
    const requestId = headers.get("X-Request-ID");
    setDetail("#d-request-id", requestId || "–");
    copyButton.hidden = !requestId;
    const usage = data.usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
    setDetail(
      "#d-tokens",
      `${fmtInt(usage.prompt_tokens)} in + ${fmtInt(usage.completion_tokens)} out = ${fmtInt(usage.total_tokens)}`,
    );
    const cost = headers.get("X-Cost-USD");
    setDetail("#d-cost", cost === null ? "–" : fmtUsd(cost));
    setDetail("#d-model", data.model || "–");
    sendStatus.textContent = "Done";
    send.disabled = false;
  } catch (error) {
    showReply("No reply.", true);
    showError(errorBox, error);
    sendStatus.textContent = "";
    if (error.status === 429 && error.retryAfter) {
      startCooldown(error.retryAfter);
    } else {
      send.disabled = false;
    }
  }
});

onKeyChange(loadModels);
loadModels();
