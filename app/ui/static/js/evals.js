import { $, api, cacheBadge, el, fmtDateTime, fmtInt, fmtMs, fmtPct, fmtUsd, getKey, hideAlert, icon, onKeyChange, showError } from "./common.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const view = $("#evals-view");
const errorBox = $("#evals-error");

function head(title, lede, ...extra) {
  return el("div", { class: "page-head" }, el("div", {}, el("h1", { text: title }), el("p", { class: "lede", text: lede })), ...extra);
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/** Green at 100%, amber in between, rose below half. Always shown with the number. */
function rateTone(rate) {
  if (rate === null || rate === undefined) return "var(--surface-3)";
  if (rate >= 1) return "var(--pass-fg)";
  if (rate >= 0.5) return "var(--warn-fg)";
  return "var(--fail-fg)";
}

/** A pass-rate ring with the percentage in the middle (decorative ring, text label). */
function ring(rate, large = false) {
  const r = 26;
  const length = 2 * Math.PI * r;
  const filled = Math.max(0, Math.min(1, rate || 0)) * length;
  const graphic = svg("svg", { viewBox: "0 0 64 64", "aria-hidden": "true", focusable: "false" });
  graphic.append(svg("circle", { class: "track", cx: 32, cy: 32, r }));
  if (filled > 0) {
    const value = svg("circle", { class: "value", cx: 32, cy: 32, r, "stroke-dasharray": `${filled} ${length}` });
    value.style.stroke = rateTone(rate);
    graphic.append(value);
  }
  return el("span", { class: large ? "ring ring-lg" : "ring" }, graphic, el("span", { class: "ring-label", text: fmtPct(rate) }));
}

function statusBadge(status) {
  const cls = status === "completed" ? "badge badge-bypass" : status === "failed" ? "badge badge-fail" : "badge badge-coalesced";
  return el("span", { class: cls, text: status });
}

function resultBadge(passed) {
  return el("span", { class: passed ? "badge badge-pass" : "badge badge-fail", text: passed ? "✓ Pass" : "✕ Fail" });
}

/** A table cell that carries its column name, for the stacked phone layout. */
function td(label, attrs, ...children) {
  return el("td", { ...attrs, "data-label": label }, ...children);
}

function runHref(id) {
  return `#run-${id}`;
}

function runCard(run) {
  const outcome = run.pass_rate === null ? "no results" : `${run.passed} of ${run.cases} passed`;
  return el("a", { class: "card run-card", href: runHref(run.id), "aria-label": `Run ${run.id}: ${run.dataset} on ${run.model}, ${outcome}` },
    ring(run.pass_rate),
    el("span", { class: "run-body" },
      el("span", { class: "run-title", text: run.dataset }),
      el("span", { class: "run-meta" },
        el("span", { class: "run-id", text: `#${run.id}` }),
        el("span", { class: "mono", text: run.model }),
        statusBadge(run.status),
      ),
      el("span", { class: "run-meta", text: outcome }),
      el("span", { class: "run-meta", text: fmtDateTime(run.finished_at || run.started_at) }),
    ),
  );
}

async function showList() {
  document.title = "Evals · LLM Gateway";
  const { data } = await api("/v1/evals/runs?limit=50");
  const intro = head("Evals", "Eval runs made with your API key, newest first.");
  if (!data.runs.length) {
    view.replaceChildren(
      intro,
      el("section", { class: "card empty" },
        el("p", { text: "No eval runs yet. Runs made with your API key show up here, for example:" }),
        el("p", {}, el("code", { text: "EVAL_API_KEY=llmgw_… python -m app.evals run --dataset mock_smoke --model mock-1" })),
      ),
    );
    return;
  }
  view.replaceChildren(
    intro,
    el("section", { class: "runs", "aria-label": "Recent eval runs" }, data.runs.map(runCard)),
  );
}

function stat(label, value, iconName, sub = null) {
  return el("div", { class: "card stat" },
    el("span", { class: "stat-icon" }, icon(iconName)),
    el("p", { class: "stat-value", text: value }),
    el("p", { class: "stat-label", text: label }),
    sub ? el("p", { class: "stat-sub", text: sub }) : null,
  );
}

async function showRun(id) {
  const { data } = await api(`/v1/evals/runs/${id}`);
  const { run, summary, results } = data;
  document.title = `Run #${run.id} · Evals · LLM Gateway`;

  const back = el("a", { href: "#", class: "btn btn-quiet btn-small", text: "← All runs" });
  const caseRows = results.map((r) =>
    el("tr", { class: r.passed ? "case-pass" : "case-fail" },
      td("Case", { class: "mono", text: r.case_id }),
      td("Scorer", { text: r.scorer }),
      td("Result", {}, resultBadge(r.passed)),
      td("Score", { class: "num", text: r.score.toFixed(2) }),
      td("Latency", { class: "num", text: fmtMs(r.latency_ms) }),
      td("Tokens", { class: "num", text: fmtInt(r.prompt_tokens + r.completion_tokens) }),
      td("Cost", { class: "num", text: fmtUsd(r.cost_usd) }),
      td("Cache", {}, r.cache_status ? cacheBadge(r.cache_status) : el("span", { class: "muted", text: "–" })),
      td("Notes", {},
        r.error ? el("span", { class: "muted", text: r.error }) : r.reason ? el("span", { text: r.reason }) : null,
        r.output !== null && r.output !== undefined
          ? el("details", { class: "case-output" }, el("summary", { text: "Answer" }), el("pre", { text: r.output }))
          : null,
      ),
    ),
  );

  view.replaceChildren(
    head(`Run #${run.id}`, `${run.dataset} on ${run.model}${run.judge_model ? `, judged by ${run.judge_model}` : ""} · ${fmtDateTime(run.finished_at || run.started_at)}`, back),
    el("section", { class: "stats", "aria-label": "Summary" },
      el("div", { class: "card stat summary-hero" },
        ring(run.pass_rate, true),
        el("div", {},
          el("p", { class: "stat-value", text: `${run.passed}/${run.cases}` }),
          el("p", { class: "stat-label", text: "cases passed" }),
        ),
      ),
      stat("Mean score", summary.mean_score.toFixed(3), "check-circle"),
      stat("Latency (median)", fmtMs(summary.p50_ms), "clock", `p95 ${fmtMs(summary.p95_ms)}`),
      stat("Cost", fmtUsd(Number(summary.cost_usd) + Number(summary.judge_cost_usd)), "dollar"),
      stat("Errors", fmtInt(summary.errors), "alert"),
    ),
    el("section", { class: "card" },
      el("h2", { text: "Cases" }),
      el("div", { class: "table-wrap" },
        el("table", { class: "stacked" },
          el("caption", { class: "visually-hidden", text: `Cases in run ${run.id}` }),
          el("thead", {}, el("tr", {},
            ["Case", "Scorer", "Result", "Score", "Latency", "Tokens", "Cost", "Cache", "Notes"].map((h, i) =>
              el("th", { scope: "col", class: i >= 3 && i <= 6 ? "num" : null, text: h }),
            ),
          )),
          el("tbody", {}, caseRows),
        ),
      ),
    ),
  );
  $("h1")?.setAttribute("tabindex", "-1");
  $("h1")?.focus();
}

async function render() {
  if (!getKey()) {
    view.replaceChildren(head("Evals", "Eval runs made with your API key."), el("section", { class: "card empty", text: "Set your API key to see your eval runs." }));
    return;
  }
  const match = /^#run-(\d+)$/.exec(location.hash);
  try {
    if (match) await showRun(Number(match[1]));
    else await showList();
    hideAlert(errorBox);
  } catch (error) {
    view.replaceChildren();
    showError(errorBox, error);
  }
}

window.addEventListener("hashchange", render);
onKeyChange(render);
render();
