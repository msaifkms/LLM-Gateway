import { $, api, ApiError, el, fmtInt, fmtMs, fmtUsd, getKey, hideAlert, onKeyChange, showError } from "./common.js";

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

const PREFERRED_MODEL = "mock-1"; // free and always available
let cooldownTimer = null;

function setDetail(id, value) {
  $(id).replaceChildren(value instanceof Node ? value : document.createTextNode(value));
}

function resetDetails() {
  for (const id of ["#d-provider", "#d-cache", "#d-latency", "#d-request-id", "#d-tokens", "#d-cost", "#d-model"]) {
    setDetail(id, "–");
  }
}

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
  reply.classList.add("placeholder");
  reply.textContent = "Waiting for the gateway…";
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
    reply.classList.remove("placeholder");
    reply.textContent = choice ? choice.message.content : "(no answer)";

    const cache = headers.get("X-Cache") || "–";
    // A cache hit was answered by no provider at all.
    setDetail("#d-provider", headers.get("X-Provider") || (cache === "HIT" ? "none (served from cache)" : "–"));
    setDetail("#d-cache", el("span", { class: cache === "HIT" ? "badge badge-accent" : "badge", text: cache }));
    setDetail("#d-latency", fmtMs(ms));
    setDetail("#d-request-id", headers.get("X-Request-ID") || "–");
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
    reply.classList.add("placeholder");
    reply.textContent = "No reply.";
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
