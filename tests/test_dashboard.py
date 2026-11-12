"""The Grafana dashboard and Prometheus config must match the real metrics."""

import json
import re
import runpy
from pathlib import Path
from typing import Any

from app.core import metrics

OPS = Path(__file__).resolve().parents[1] / "ops"
DASHBOARD = OPS / "grafana" / "dashboards" / "llm-gateway.json"
BUILDER = OPS / "grafana" / "build_dashboard.py"
DATASOURCE = OPS / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
PROMETHEUS = OPS / "prometheus" / "prometheus.yml"

SUFFIXES = ("_total", "_bucket", "_count", "_sum", "_created")


def base_name(name: str) -> str:
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DASHBOARD.read_text())
    return data


def queries(dashboard: dict[str, Any]) -> list[str]:
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]


def used_metrics(dashboard: dict[str, Any]) -> set[str]:
    names = re.findall(r"gateway_[a-z_]+", " ".join(queries(dashboard)))
    return {base_name(name) for name in names}


def gateway_metrics() -> set[str]:
    return {
        family.name
        for family in metrics.REGISTRY.collect()
        if family.name.startswith("gateway_")
    }


def test_dashboard_file_is_up_to_date_with_its_generator() -> None:
    build = runpy.run_path(str(BUILDER))["build"]
    assert build() == load(), "run: python ops/grafana/build_dashboard.py"


def test_panel_ids_are_unique() -> None:
    ids = [panel["id"] for panel in load()["panels"]]
    assert len(ids) == len(set(ids))


def test_every_query_uses_a_real_metric() -> None:
    unknown = used_metrics(load()) - gateway_metrics()
    assert not unknown, f"dashboard uses metrics that do not exist: {unknown}"


def test_every_metric_appears_on_the_dashboard() -> None:
    missing = gateway_metrics() - used_metrics(load())
    assert not missing, f"metrics not shown on the dashboard: {missing}"


def test_panels_use_the_provisioned_datasource() -> None:
    assert "uid: prometheus" in DATASOURCE.read_text()
    for panel in load()["panels"]:
        if panel["type"] != "row":
            assert panel["datasource"]["uid"] == "prometheus"


def test_prometheus_scrapes_the_gateway() -> None:
    config = PROMETHEUS.read_text()
    assert '"gateway:8000"' in config
    assert "metrics_path: /metrics" in config
