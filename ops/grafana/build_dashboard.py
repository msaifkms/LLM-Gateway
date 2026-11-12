"""Build the Grafana dashboard JSON from a short Python description.

Run from the repository root after changing a panel:

    python ops/grafana/build_dashboard.py

Grafana dashboards are long, repetitive JSON. Generating it keeps the source
readable and reviewable, and the generated file is what Grafana loads.
"""

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "dashboards" / "llm-gateway.json"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}
CHAT = 'route="/v1/chat/completions"'

Panel = dict[str, Any]


def _targets(queries: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "refId": chr(65 + i),
            "datasource": DATASOURCE,
            "expr": expr,
            "legendFormat": legend,
        }
        for i, (expr, legend) in enumerate(queries)
    ]


def graph(
    title: str, queries: list[tuple[str, str]], unit: str = "short", width: int = 12
) -> Panel:
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": {"h": 8, "w": width},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi"},
        },
        "targets": _targets(queries),
    }


def stat(title: str, expr: str, unit: str = "short", **defaults: Any) -> Panel:
    return {
        "type": "stat",
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": {"h": 4, "w": 6},
        "fieldConfig": {"defaults": {"unit": unit, **defaults}, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
        },
        "targets": _targets([(expr, "")]),
    }


def row(title: str) -> Panel:
    return {
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24},
        "panels": [],
    }


def ratio(numerator: str, denominator: str) -> str:
    # A missing numerator counts as 0 (no 5xx series yet means 0%), and
    # clamp_min avoids dividing by zero when there is no traffic yet.
    return f"({numerator} or vector(0)) / clamp_min({denominator}, 1e-9)"


def quantile(q: float, metric: str, by: str = "le", selector: str = "") -> str:
    return (
        f"histogram_quantile({q}, sum by ({by}) "
        f"(rate({metric}_bucket{{{selector}}}[5m])))"
    )


LATENCY = "gateway_http_request_duration_seconds"
PROVIDER_ATTEMPTS = (
    "sum by (provider, outcome) (rate(gateway_provider_attempts_total[1m]))"
)
KILL_SWITCH_DEFAULTS = {
    "mappings": [
        {"type": "value", "options": {"0": {"text": "OFF"}, "1": {"text": "ON"}}}
    ],
    "thresholds": {
        "mode": "absolute",
        "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}],
    },
}

LAYOUT: list[list[Panel]] = [
    [row("Overview")],
    [
        stat(
            "Chat requests / s",
            f"sum(rate(gateway_http_requests_total{{{CHAT}}}[1m]))",
            "reqps",
        ),
        stat(
            "Error rate (5xx)",
            ratio(
                f'sum(rate(gateway_http_requests_total{{{CHAT},status=~"5.."}}[5m]))',
                f"sum(rate(gateway_http_requests_total{{{CHAT}}}[5m]))",
            ),
            "percentunit",
        ),
        stat(
            "Cache hit ratio",
            ratio(
                'sum(rate(gateway_cache_requests_total{result=~"HIT|COALESCED"}[5m]))',
                "sum(rate(gateway_cache_requests_total[5m]))",
            ),
            "percentunit",
        ),
        stat("Kill switch", "gateway_kill_switch_on", **KILL_SWITCH_DEFAULTS),
    ],
    [
        graph(
            "Chat requests by status",
            [
                (
                    f"sum by (status) (rate(gateway_http_requests_total{{{CHAT}}}[1m]))",
                    "{{status}}",
                )
            ],
            "reqps",
        ),
        graph(
            "Chat latency",
            [
                (quantile(0.5, LATENCY, selector=CHAT), "p50"),
                (quantile(0.95, LATENCY, selector=CHAT), "p95"),
                (quantile(0.99, LATENCY, selector=CHAT), "p99"),
            ],
            "s",
        ),
    ],
    [row("Cache and providers")],
    [
        graph(
            "Cache outcomes",
            [
                (
                    "sum by (result) (rate(gateway_cache_requests_total[1m]))",
                    "{{result}}",
                )
            ],
            "reqps",
        ),
        graph(
            "Provider attempts by outcome",
            [
                (
                    PROVIDER_ATTEMPTS,
                    "{{provider}} {{outcome}}",
                )
            ],
            "reqps",
        ),
    ],
    [
        graph(
            "Provider p95 latency",
            [
                (
                    quantile(
                        0.95,
                        "gateway_provider_attempt_duration_seconds",
                        by="le, provider",
                    ),
                    "{{provider}}",
                )
            ],
            "s",
        ),
        graph(
            "Circuit breaker state (0 closed, 1 half-open, 2 open)",
            [("gateway_circuit_breaker_state", "{{provider}}")],
        ),
    ],
    [row("Protection")],
    [
        graph(
            "Rate limiter",
            [
                (
                    "sum by (outcome) (rate(gateway_rate_limit_events_total[1m]))",
                    "{{outcome}}",
                )
            ],
            "reqps",
            width=8,
        ),
        graph(
            "Quota rejections",
            [
                (
                    "sum by (reason) (rate(gateway_quota_rejections_total[1m]))",
                    "{{reason}}",
                )
            ],
            "reqps",
            width=8,
        ),
        graph(
            "Kill switch blocks",
            [
                (
                    "sum by (reason) (rate(gateway_kill_switch_blocks_total[1m]))",
                    "{{reason}}",
                )
            ],
            "reqps",
            width=8,
        ),
    ],
    [row("Spend")],
    [
        graph(
            "Cost per hour (USD)",
            [
                (
                    "sum by (model) (increase(gateway_cost_usd_total[1h]))",
                    "billed {{model}}",
                ),
                (
                    "sum by (model) (increase(gateway_cost_saved_usd_total[1h]))",
                    "saved {{model}}",
                ),
            ],
            "currencyUSD",
        ),
        graph(
            "Tokens / s",
            [
                (
                    "sum by (model, kind) (rate(gateway_tokens_total[1m]))",
                    "{{model}} {{kind}}",
                )
            ],
        ),
    ],
    [row("Failures")],
    [
        graph(
            "Fallbacks",
            [
                (
                    "sum by (provider) (rate(gateway_fallbacks_total[5m]))",
                    "{{provider}}",
                )
            ],
            "reqps",
            width=8,
        ),
        graph(
            "Redis outages and usage-write failures",
            [
                (
                    "sum by (component) (increase(gateway_redis_outages_total[15m]))",
                    "redis {{component}}",
                ),
                (
                    "increase(gateway_usage_write_failures_total[15m])",
                    "usage writes",
                ),
            ],
            width=8,
        ),
        graph(
            "Unpriced billed requests",
            [
                (
                    "sum by (model) (increase(gateway_unpriced_requests_total[1h]))",
                    "{{model}}",
                )
            ],
            width=8,
        ),
    ],
]


def build() -> dict[str, Any]:
    panels: list[Panel] = []
    y = 0
    for line in LAYOUT:
        x = 0
        for panel in line:
            panel["gridPos"].update({"x": x, "y": y})
            panel["id"] = len(panels) + 1
            x += panel["gridPos"]["w"]
            panels.append(panel)
        y += max(panel["gridPos"]["h"] for panel in line)
    return {
        "uid": "llm-gateway",
        "title": "LLM Gateway",
        "tags": ["llm-gateway"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "editable": True,
        "panels": panels,
        "templating": {"list": []},
        "annotations": {"list": []},
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"Wrote {OUT}")
