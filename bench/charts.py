"""Charts for docs/benchmarks/ (SVG, matplotlib). Only the report needs these."""

from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display needed
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# A validated categorical palette (fixed order, never cycled) and neutral ink.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e3e2de"
SURFACE = "#ffffff"

plt.rcParams.update(
    {
        "font.family": ["Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 10,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_2,
        "axes.titlecolor": INK,
        "axes.titlesize": 12,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "legend.frameon": False,
        "legend.labelcolor": INK_2,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "svg.hashsalt": "llm-gateway",  # stable SVG ids, so reruns diff cleanly
        "svg.fonttype": "none",  # text stays text
    }
)


def _ms(value: float, _pos: object = None) -> str:
    if value >= 1000:
        return f"{value / 1000:g} s"
    return f"{value:g} ms"


def _save(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, format="svg", metadata={"Date": None})
    plt.close(fig)


def grouped_bars(
    path: Path,
    title: str,
    categories: Sequence[str],
    series: dict[str, Sequence[float]],
    *,
    ylabel: str,
    log: bool = False,
    value_format: str = "{:,.0f}",
) -> None:
    """Bars grouped by category, one colour per series, every bar labelled."""
    fig, ax = plt.subplots(figsize=(7, 3.6))
    n = len(series)
    width = 0.8 / n
    for i, (name, values) in enumerate(series.items()):
        xs = [c + (i - (n - 1) / 2) * width for c in range(len(categories))]
        bars = ax.bar(xs, values, width * 0.92, label=name, color=SERIES[i], zorder=2)
        ax.bar_label(
            bars,
            labels=[value_format.format(v) for v in values],
            padding=2,
            fontsize=8,
            color=INK_2,
        )
    ax.set_xticks(range(len(categories)), categories)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="x", visible=False)
    if log:
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(_ms))
    ax.margins(y=0.15)
    if n > 1:
        ax.legend(loc="upper left", ncols=n)
    _save(fig, path)


def resilience_timeline(
    path: Path,
    per_request_ms: Sequence[float],
    reached_primary: Sequence[bool],
    baseline_p50_ms: float,
) -> None:
    """Latency of each request in order, marking those that tried the failing primary."""
    fig, ax = plt.subplots(figsize=(7, 3.4))
    tried = [i for i, t in enumerate(reached_primary) if t]
    others = [i for i in range(len(per_request_ms)) if i not in set(tried)]
    ax.scatter(
        others,
        [per_request_ms[i] for i in others],
        s=10,
        color=SERIES[0],
        label="answered by the fallback (breaker open)",
        zorder=2,
    )
    ax.scatter(
        tried,
        [per_request_ms[i] for i in tried],
        s=36,
        color=SERIES[1],
        label="tried the failing primary first",
        zorder=3,
        edgecolors=SURFACE,
        linewidths=1,
    )
    ax.axhline(
        baseline_p50_ms,
        color=INK_2,
        linewidth=1,
        linestyle="--",
        zorder=1,
        label=f"healthy route, p50 {baseline_p50_ms:.1f} ms",
    )
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(FuncFormatter(_ms))
    ax.set_xlabel("request number (one at a time)")
    ax.set_ylabel("latency")
    ax.set_title("Primary down: latency of each request")
    ax.legend(loc="upper right")
    _save(fig, path)


def rate_limit_timeline(
    path: Path,
    allowed_per_second: Sequence[int],
    limit_per_min: int,
    offered_rps: float,
) -> None:
    """Cumulative allowed requests against what the token bucket should allow."""
    fig, ax = plt.subplots(figsize=(7, 3.4))
    cumulative = []
    total = 0
    for count in allowed_per_second:
        total += count
        cumulative.append(total)
    seconds = list(range(1, len(cumulative) + 1))
    rate = limit_per_min / 60
    # Requests can only be allowed as fast as they are offered: the full bucket
    # drains at the offered rate, then refill sets the pace.
    expected = [min(offered_rps * t, limit_per_min + rate * t) for t in seconds]
    ax.plot(
        seconds,
        expected,
        color=SERIES[1],
        linewidth=2,
        linestyle="--",
        label=f"expected: bucket of {limit_per_min} at the offered rate, then {rate:g}/s",
    )
    ax.step(
        seconds,
        cumulative,
        where="post",
        color=SERIES[0],
        linewidth=2,
        label="allowed (measured)",
    )
    ax.set_xlabel("seconds since the first request")
    ax.set_ylabel("requests allowed so far")
    ax.set_title(f"Rate limit {limit_per_min}/min under {offered_rps:g} requests/s")
    ax.legend(loc="lower right")
    _save(fig, path)
