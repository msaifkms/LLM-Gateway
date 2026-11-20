import { $, api, el, fmtInt, fmtUsd, getKey, hideAlert, onKeyChange, showError } from "./common.js";

const SVG = "http://www.w3.org/2000/svg";
const errorBox = $("#usage-error");
const stats = $("#stats");
const chartBox = $("#chart");
const metricSelect = $("#metric");
const chartTitle = $("#chart-title");

const METRICS = {
  requests: { title: "Requests per day", value: (d) => d.requests, format: (v) => fmtInt(v) },
  tokens: { title: "Tokens per day", value: (d) => d.prompt_tokens + d.completion_tokens, format: (v) => fmtInt(v) },
  cost: { title: "Cost per day (USD)", value: (d) => Number(d.cost_usd), format: (v) => fmtUsd(v) },
};

let days = [];
let current = null;

function selectedDays() {
  return Number(document.querySelector('input[name="days"]:checked').value);
}

/** Every UTC day the window covers, with zeros where nothing happened. */
function fillDays(byDay, window) {
  const seen = new Map(byDay.map((d) => [d.day, d]));
  const end = new Date();
  const start = new Date(end.getTime() - window * 86_400_000);
  const out = [];
  for (let t = Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate()); t <= end.getTime(); t += 86_400_000) {
    const day = new Date(t).toISOString().slice(0, 10);
    out.push(seen.get(day) || { day, requests: 0, prompt_tokens: 0, completion_tokens: 0, cost_usd: "0" });
  }
  return out;
}

function dayLabel(iso, withYear = false) {
  const date = new Date(`${iso}T00:00:00Z`);
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: withYear ? "numeric" : undefined, timeZone: "UTC" });
}

function stat(label, value, sub) {
  return el("div", { class: "card stat" },
    el("p", { class: "stat-label", text: label }),
    el("p", { class: "stat-value", text: value }),
    sub ? el("p", { class: "stat-sub", text: sub }) : null,
  );
}

function renderStats(totals) {
  const tokens = totals.prompt_tokens + totals.completion_tokens;
  stats.replaceChildren(
    stat("Requests", fmtInt(totals.requests), `${fmtInt(totals.cache_served)} served from cache`),
    stat("Tokens", fmtInt(tokens), `${fmtInt(totals.prompt_tokens)} in, ${fmtInt(totals.completion_tokens)} out`),
    stat("Cost", fmtUsd(totals.cost_usd), "what you were charged"),
    stat("Saved by cache", fmtUsd(totals.saved_usd), "cost avoided by cache hits"),
  );
}

const TICKS = 4;

/** A round step (1, 2, 2.5 or 5 times a power of 10) so TICKS steps cover max. */
function niceStep(max, whole) {
  if (max <= 0) return whole ? 1 : 0.25;
  const raw = max / TICKS;
  const power = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) {
    const step = m * power;
    if (step >= raw && (!whole || Number.isInteger(step))) return step;
  }
  return 10 * power;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/** A bar whose top corners are rounded and whose base sits flat on the axis. */
function barPath(x, y, w, h) {
  const r = Math.min(4, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function renderChart() {
  const metric = METRICS[metricSelect.value];
  chartTitle.textContent = metric.title;
  const values = days.map(metric.value);
  const width = Math.max(chartBox.clientWidth, 280);
  const height = 240;
  const pad = { top: 12, right: 8, bottom: 28, left: 56 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const step = niceStep(Math.max(...values, 0), metricSelect.value !== "cost");
  const top = step * TICKS;
  const slot = plotW / Math.max(values.length, 1);
  const barW = Math.max(2, Math.min(48, slot - 2)); // 2px surface gap between bars

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", tabindex: 0 });
  const total = values.reduce((a, b) => a + b, 0);
  root.setAttribute(
    "aria-label",
    `${metric.title}, ${days.length} days, total ${metric.format(total)}. Use the left and right arrow keys to read each day, or open the table below.`,
  );

  const grid = svg("g", { class: "grid" });
  const axis = svg("g", { class: "axis" });
  for (let i = 0; i <= TICKS; i += 1) {
    const v = step * i;
    const y = pad.top + plotH - (v / top) * plotH;
    grid.append(svg("line", { x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
    const label = svg("text", { x: pad.left - 8, y: y + 4, "text-anchor": "end" });
    label.textContent = metric.format(v);
    axis.append(label);
  }

  // Label at most ~7 days so the dates never collide.
  const every = Math.max(1, Math.ceil(values.length / Math.max(1, Math.floor(plotW / 70))));
  const bars = svg("g");
  days.forEach((d, i) => {
    const v = values[i];
    const h = top > 0 ? (v / top) * plotH : 0;
    const x = pad.left + i * slot + (slot - barW) / 2;
    const y = pad.top + plotH - h;
    const group = svg("g", { class: "bar-group", "data-index": i });
    if (h > 0) group.append(svg("path", { class: "bar", d: barPath(x, y, barW, Math.max(h, 1)) }));
    // A hit area the full height of the slot, bigger than the bar itself.
    group.append(svg("rect", { class: "bar-hit", x: pad.left + i * slot, y: pad.top, width: slot, height: plotH }));
    bars.append(group);
    if (i % every === 0 || i === values.length - 1) {
      const label = svg("text", { x: x + barW / 2, y: height - 8, "text-anchor": "middle" });
      label.textContent = dayLabel(d.day);
      axis.append(label);
    }
  });
  grid.append(svg("line", { x1: pad.left, x2: width - pad.right, y1: pad.top + plotH, y2: pad.top + plotH }));

  root.append(grid, bars, axis);
  const tip = el("div", { class: "tooltip", hidden: true, role: "status" });

  const show = (i) => {
    if (i === null || i < 0 || i >= days.length) {
      tip.hidden = true;
      bars.querySelectorAll(".active").forEach((g) => g.classList.remove("active"));
      current = null;
      return;
    }
    current = i;
    bars.querySelectorAll(".bar-group").forEach((g) => g.classList.toggle("active", Number(g.dataset.index) === i));
    tip.replaceChildren(el("strong", { text: metric.format(values[i]) }), el("span", { text: dayLabel(days[i].day, true) }));
    const xPx = ((pad.left + i * slot + slot / 2) / width) * chartBox.clientWidth;
    const yPx = ((pad.top + plotH - (top > 0 ? (values[i] / top) * plotH : 0)) / height) * root.getBoundingClientRect().height;
    // Keep the tooltip inside the chart: below the point near the top edge,
    // and clear of the left and right edges.
    tip.classList.toggle("below", yPx < 64);
    const half = 70;
    tip.style.left = `${Math.min(Math.max(xPx, half), chartBox.clientWidth - half)}px`;
    tip.style.top = `${yPx}px`;
    tip.hidden = false;
  };

  bars.addEventListener("pointermove", (event) => {
    const group = event.target.closest(".bar-group");
    show(group ? Number(group.dataset.index) : null);
  });
  root.addEventListener("pointerleave", () => show(null));
  root.addEventListener("blur", () => show(null));
  root.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const start = current === null ? (step > 0 ? -1 : days.length) : current;
      show(Math.min(days.length - 1, Math.max(0, start + step)));
    } else if (event.key === "Escape") {
      show(null);
    }
  });

  chartBox.replaceChildren(root, tip);
}

function table(headers, rows, numeric) {
  return el("table", {},
    el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { class: numeric.includes(i) ? "num" : null, scope: "col", text: h })))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => el("td", { class: numeric.includes(i) ? "num" : null, text: c }))))),
  );
}

function renderTables(summary) {
  $("#day-table").replaceChildren(
    table(
      ["Day (UTC)", "Requests", "Tokens", "Cost"],
      days.map((d) => [d.day, fmtInt(d.requests), fmtInt(d.prompt_tokens + d.completion_tokens), fmtUsd(d.cost_usd)]),
      [1, 2, 3],
    ),
  );
  const models = $("#model-table");
  if (!summary.by_model.length) {
    models.replaceChildren(el("p", { class: "empty", text: "No requests in this period yet. Try the Playground." }));
    return;
  }
  models.replaceChildren(
    table(
      ["Model", "Requests", "Prompt tokens", "Completion tokens", "Cost"],
      summary.by_model.map((m) => [m.model, fmtInt(m.requests), fmtInt(m.prompt_tokens), fmtInt(m.completion_tokens), fmtUsd(m.cost_usd)]),
      [1, 2, 3, 4],
    ),
  );
}

async function load() {
  if (!getKey()) {
    stats.replaceChildren();
    chartBox.replaceChildren(el("p", { class: "empty", text: "Set your API key to see your usage." }));
    return;
  }
  const window = selectedDays();
  try {
    const { data } = await api(`/v1/usage?days=${window}`);
    hideAlert(errorBox);
    days = fillDays(data.by_day, window);
    renderStats(data.totals);
    renderChart();
    renderTables(data);
  } catch (error) {
    showError(errorBox, error);
  }
}

document.querySelectorAll('input[name="days"]').forEach((input) => input.addEventListener("change", load));
metricSelect.addEventListener("change", () => days.length && renderChart());
let resizeFrame = null;
new ResizeObserver(() => {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => days.length && renderChart());
}).observe(chartBox);
onKeyChange(load);
load();
