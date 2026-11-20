import { $, api, el, fmtDateTime, fmtInt, fmtMs, fmtPct, fmtUsd, getKey, hideAlert, onKeyChange, showError } from "./common.js";

const view = $("#evals-view");
const errorBox = $("#evals-error");

function head(title, lede, ...extra) {
  return el("div", { class: "page-head" }, el("div", {}, el("h1", { text: title }), el("p", { class: "lede", text: lede })), ...extra);
}

function meter(rate, label) {
  const fill = el("div", { class: "meter-fill" });
  fill.style.width = `${Math.round((rate || 0) * 100)}%`;
  return el("span", { class: "meter" }, el("span", { class: "meter-track", "aria-hidden": "true" }, fill), el("span", { text: label }));
}

function statusBadge(status) {
  const cls = status === "completed" ? "badge" : status === "failed" ? "badge badge-fail" : "badge badge-accent";
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

  const rows = data.runs.map((run) => {
    const link = el("a", { href: runHref(run.id), text: `#${run.id}` });
    const row = el("tr", { class: "clickable" },
      td("Run", {}, link),
      td("Dataset", { text: run.dataset }),
      td("Model", { class: "mono", text: run.model }),
      td("Pass rate", {}, run.pass_rate === null ? el("span", { class: "muted", text: "–" }) : meter(run.pass_rate, `${fmtPct(run.pass_rate)} (${run.passed}/${run.cases})`)),
      td("Status", {}, statusBadge(run.status)),
      td("Date", { class: "muted", text: fmtDateTime(run.finished_at || run.started_at) }),
    );
    // The whole row opens the run; the link inside stays the keyboard target.
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      location.hash = runHref(run.id);
    });
    return row;
  });

  view.replaceChildren(
    intro,
    el("section", { class: "card" },
      el("div", { class: "table-wrap" },
        el("table", { class: "stacked" },
          el("caption", { class: "visually-hidden", text: "Recent eval runs" }),
          el("thead", {}, el("tr", {}, ["Run", "Dataset", "Model", "Pass rate", "Status", "Date"].map((h) => el("th", { scope: "col", text: h })))),
          el("tbody", {}, rows),
        ),
      ),
    ),
  );
}

function stat(label, value) {
  return el("div", { class: "card stat" }, el("p", { class: "stat-label", text: label }), el("p", { class: "stat-value", text: value }));
}

async function showRun(id) {
  const { data } = await api(`/v1/evals/runs/${id}`);
  const { run, summary, results } = data;
  document.title = `Run #${run.id} · Evals · LLM Gateway`;

  const back = el("a", { href: "#", class: "btn btn-quiet btn-small", text: "← All runs" });
  const caseRows = results.map((r) =>
    el("tr", {},
      td("Case", { class: "mono", text: r.case_id }),
      td("Scorer", { text: r.scorer }),
      td("Result", {}, resultBadge(r.passed)),
      td("Score", { class: "num", text: r.score.toFixed(2) }),
      td("Latency", { class: "num", text: fmtMs(r.latency_ms) }),
      td("Tokens", { class: "num", text: fmtInt(r.prompt_tokens + r.completion_tokens) }),
      td("Cost", { class: "num", text: fmtUsd(r.cost_usd) }),
      td("Cache", { class: "mono", text: r.cache_status || "–" }),
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
      stat("Pass rate", `${fmtPct(run.pass_rate)} (${run.passed}/${run.cases})`),
      stat("Mean score", summary.mean_score.toFixed(3)),
      stat("Latency p50 / p95", `${fmtMs(summary.p50_ms)} / ${fmtMs(summary.p95_ms)}`),
      stat("Cost", fmtUsd(Number(summary.cost_usd) + Number(summary.judge_cost_usd))),
      stat("Errors", fmtInt(summary.errors)),
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
