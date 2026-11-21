import { $, api, el, fmtInt, fmtUsd, getKey, hideAlert, icon, onKeyChange, showError } from "./common.js";

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

function stat(label, value, sub, iconName, hero = false) {
  return el("div", { class: hero ? "card stat stat-hero" : "card stat" },
    el("span", { class: "stat-icon" }, icon(iconName)),
    el("p", { class: "stat-value", text: value }),
    el("p", { class: "stat-label", text: label }),
    sub ? el("p", { class: "stat-sub", text: sub }) : null,
  );
}

function renderStats(totals) {
  const tokens = totals.prompt_tokens + totals.completion_tokens;
  stats.replaceChildren(
    stat("Requests", fmtInt(totals.requests), `${fmtInt(totals.cache_served)} served from cache`, "list", true),
    stat("Tokens", fmtInt(tokens), `${fmtInt(totals.prompt_tokens)} in, ${fmtInt(totals.completion_tokens)} out`, "layers"),
    stat("Cost", fmtUsd(totals.cost_usd), "what you were charged", "dollar"),
    stat("Saved by cache", fmtUsd(totals.saved_usd), "cost avoided by cache hits", "piggy"),
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

/**
 * A smooth path through the points (monotone cubic, Fritsch-Carlson): it never
 * overshoots, so a day of zero never dips below the axis between two busy days.
 */
function monotonePath(points) {
  const n = points.length;
  if (n === 0) return "";
  if (n === 1) return `M${points[0][0]},${points[0][1]}`;
  const dx = [];
  const slope = [];
  for (let i = 0; i < n - 1; i += 1) {
    dx.push(points[i + 1][0] - points[i][0]);
    slope.push((points[i + 1][1] - points[i][1]) / dx[i]);
  }
  const tangent = [slope[0]];
  for (let i = 1; i < n - 1; i += 1) {
    tangent.push(slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2);
  }
  tangent.push(slope[n - 2]);
  for (let i = 0; i < n - 1; i += 1) {
    if (slope[i] === 0) {
      tangent[i] = 0;
      tangent[i + 1] = 0;
    } else {
      const a = tangent[i] / slope[i];
      const b = tangent[i + 1] / slope[i];
      const h = a * a + b * b;
      if (h > 9) {
        const t = 3 / Math.sqrt(h);
        tangent[i] = t * a * slope[i];
        tangent[i + 1] = t * b * slope[i];
      }
    }
  }
  let d = `M${points[0][0]},${points[0][1]}`;
  for (let i = 0; i < n - 1; i += 1) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[i + 1];
    const third = dx[i] / 3;
    d += `C${x0 + third},${y0 + tangent[i] * third} ${x1 - third},${y1 - tangent[i + 1] * third} ${x1},${y1}`;
  }
  return d;
}

/**
 * Gradients in plot coordinates (userSpaceOnUse): a bounding-box gradient
 * renders nothing on a perfectly flat line, such as a week with no traffic.
 */
function gradientDefs(left, right, top, bottom) {
  const defs = svg("defs");
  const space = { gradientUnits: "userSpaceOnUse" };
  const line = svg("linearGradient", { id: "usage-line", ...space, x1: left, y1: 0, x2: right, y2: 0 });
  line.append(svg("stop", { offset: "0%", class: "stop-a" }), svg("stop", { offset: "100%", class: "stop-b" }));
  const area = svg("linearGradient", { id: "usage-area", ...space, x1: 0, y1: top, x2: 0, y2: bottom });
  area.append(svg("stop", { offset: "0%", class: "stop-fill-top" }), svg("stop", { offset: "100%", class: "stop-fill-bottom" }));
  defs.append(line, area);
  return defs;
}

function renderChart() {
  const metric = METRICS[metricSelect.value];
  chartTitle.textContent = metric.title;
  const values = days.map(metric.value);
  const width = Math.max(chartBox.clientWidth, 280);
  const height = 260;
  const pad = { top: 16, right: 14, bottom: 30, left: 58 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const step = niceStep(Math.max(...values, 0), metricSelect.value !== "cost");
  const top = step * TICKS;
  const xAt = (i) => pad.left + (values.length > 1 ? (i / (values.length - 1)) * plotW : plotW / 2);
  const yAt = (v) => pad.top + plotH - (top > 0 ? (v / top) * plotH : 0);
  const points = values.map((v, i) => [xAt(i), yAt(v)]);

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", tabindex: 0 });
  const total = values.reduce((a, b) => a + b, 0);
  root.setAttribute(
    "aria-label",
    `${metric.title}, ${days.length} days, total ${metric.format(total)}. Use the left and right arrow keys to read each day, or open the table below.`,
  );
  root.append(gradientDefs(pad.left, width - pad.right, pad.top, pad.top + plotH));

  const grid = svg("g", { class: "grid" });
  const axis = svg("g", { class: "axis" });
  for (let i = 0; i <= TICKS; i += 1) {
    const v = step * i;
    const y = yAt(v);
    grid.append(svg("line", { x1: pad.left, x2: width - pad.right, y1: y, y2: y }));
    const label = svg("text", { x: pad.left - 10, y: y + 4, "text-anchor": "end" });
    label.textContent = metric.format(v);
    axis.append(label);
  }
  // Label evenly spaced days plus the last one, never two that would collide.
  const every = Math.max(1, Math.ceil(values.length / Math.max(1, Math.floor(plotW / 74))));
  const labelled = days.map((_, i) => i).filter((i) => i % every === 0);
  const last = values.length - 1;
  if (labelled[labelled.length - 1] !== last) {
    if (labelled.length > 1 && last - labelled[labelled.length - 1] < every) labelled.pop();
    labelled.push(last);
  }
  days.forEach((d, i) => {
    if (labelled.includes(i)) {
      const label = svg("text", { x: xAt(i), y: height - 8, "text-anchor": i === 0 && values.length > 1 ? "start" : i === values.length - 1 && values.length > 1 ? "end" : "middle" });
      label.textContent = dayLabel(d.day);
      axis.append(label);
    }
  });

  const curve = monotonePath(points);
  const baseline = pad.top + plotH;
  const area = svg("path", {
    d: `${curve}L${points[points.length - 1][0]},${baseline}L${points[0][0]},${baseline}Z`,
    fill: "url(#usage-area)",
  });
  const line = svg("path", { d: curve, class: "line", stroke: "url(#usage-line)" });
  const hoverLine = svg("line", { class: "hover-line", y1: pad.top, y2: baseline, x1: 0, x2: 0 });
  const dot = svg("circle", { class: "dot", r: 5.5, cx: 0, cy: 0 });
  const hit = svg("rect", { class: "hit", x: pad.left - 10, y: pad.top, width: plotW + 20, height: plotH });

  root.append(grid, area, line, hoverLine, dot, axis, hit);
  const tip = el("div", { class: "tooltip", hidden: true, role: "status" });

  const show = (i) => {
    if (i === null || i < 0 || i >= days.length) {
      tip.hidden = true;
      dot.classList.remove("active");
      hoverLine.classList.remove("active");
      current = null;
      return;
    }
    current = i;
    const [x, y] = points[i];
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", y);
    dot.classList.add("active");
    hoverLine.setAttribute("x1", x);
    hoverLine.setAttribute("x2", x);
    hoverLine.classList.add("active");
    tip.replaceChildren(el("strong", { text: metric.format(values[i]) }), el("span", { text: dayLabel(days[i].day, true) }));
    const scale = chartBox.clientWidth / width;
    const yPx = y * (root.getBoundingClientRect().height / height);
    tip.classList.toggle("below", yPx < 70);
    const half = 70;
    tip.style.left = `${Math.min(Math.max(x * scale, half), chartBox.clientWidth - half)}px`;
    tip.style.top = `${yPx}px`;
    tip.hidden = false;
  };

  const nearest = (clientX) => {
    const rect = root.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * width;
    let best = 0;
    for (let i = 1; i < points.length; i += 1) {
      if (Math.abs(points[i][0] - x) < Math.abs(points[best][0] - x)) best = i;
    }
    return best;
  };

  hit.addEventListener("pointermove", (event) => show(nearest(event.clientX)));
  root.addEventListener("pointerleave", () => show(null));
  root.addEventListener("blur", () => show(null));
  root.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const delta = event.key === "ArrowRight" ? 1 : -1;
      const from = current === null ? (delta > 0 ? -1 : days.length) : current;
      show(Math.min(days.length - 1, Math.max(0, from + delta)));
    } else if (event.key === "Escape") {
      show(null);
    }
  });

  chartBox.replaceChildren(root, tip);
}

function table(headers, rows, numeric) {
  // "stacked": on phones each row becomes a small card labelled by column.
  return el("table", { class: "stacked" },
    el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { class: numeric.includes(i) ? "num" : null, scope: "col", text: h })))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => el("td", { class: numeric.includes(i) ? "num" : null, "data-label": headers[i], text: c }))))),
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
