"""Control the Docker Compose stack the benchmarks run against."""

import json
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "docker-compose.yml"),
    "-f",
    str(ROOT / "bench" / "docker-compose.bench.yml"),
]
#: The gateway as the scenarios see it. Inside the Compose network (the
#: bench-client container) this is http://gateway:8000; from the host it is the
#: published port, which goes through Docker Desktop's port forwarding.
BASE_URL = os.environ.get("BENCH_BASE_URL", "http://localhost:8000")
HOST_URL = "http://localhost:8000"

#: High enough that the rate limiter (still checked on every request) never
#: refuses anything outside the rate-limit scenario.
UNLIMITED_PER_MIN = 10_000_000


def _run(args: list[str], *, quiet: bool = True) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # stderr from compose/the CLI never holds keys (issue-key prints to stdout).
        raise RuntimeError(f"{' '.join(args[:4])} ... failed:\n{result.stderr[-2000:]}")
    return result.stdout


def compose(*args: str) -> str:
    return _run([*COMPOSE, *args])


def up() -> None:
    compose("up", "-d", "--build", "--wait", "gateway")
    compose("--profile", "bench", "build", "bench-client")


def run_in_network(args: list[str], keys: dict[str, str]) -> dict[str, object]:
    """Run ``python -m bench.client ARGS`` in the bench-client container.

    The keys go in on stdin, so they never appear in a process list or the
    container's environment. The scenario prints its result as JSON.
    """
    result = subprocess.run(
        [
            *COMPOSE,
            "--profile",
            "bench",
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "bench-client",
            *args,
        ],
        cwd=ROOT,
        input=json.dumps(keys),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bench-client {args[0]} failed:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def wait_ready(timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{HOST_URL}/readyz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("the gateway did not become ready")


def restart_gateway() -> None:
    """A fresh gateway process: closed breakers, empty single-flight, zeroed metrics."""
    compose("restart", "gateway")
    wait_ready()


def migrate() -> None:
    compose("run", "--rm", "-T", "gateway", "python", "-m", "app.db.migrate")


def cli(*args: str) -> str:
    return compose("run", "--rm", "-T", "gateway", "python", "-m", "app.cli", *args)


def create_user_with_key(name: str, rate_limit: int) -> str:
    """Create a user and return a fresh key for it. The key is never printed."""
    try:
        cli("create-user", name, "--rate-limit", str(rate_limit))
    except RuntimeError as exc:
        if "already exists" not in str(exc):
            raise
        cli("set-limits", name, "--rate-limit", str(rate_limit))
    out = cli("issue-key", name, "--label", "benchmark")
    match = re.search(r"^Key\s+:\s+(\S+)$", out, re.MULTILINE)
    if match is None:
        raise RuntimeError("could not read the issued key")
    return match.group(1)


def metrics_text() -> str:
    return httpx.get(f"{BASE_URL}/metrics", timeout=10).text


@dataclass(frozen=True)
class Machine:
    info: dict[str, str]


def _cmd(args: list[str]) -> str:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=20
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def machine_info() -> dict[str, str]:
    """The host, Docker and software versions the numbers were measured on."""
    docker = _cmd(["docker", "info", "--format", "{{json .}}"])
    d = json.loads(docker) if docker.startswith("{") else {}
    mem_gib = d.get("MemTotal", 0) / 2**30
    info = {
        "host_os": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}",
        "host_model": _cmd(["sysctl", "-n", "hw.model"]),
        "host_cpu": _cmd(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "host_cpu_cores": _cmd(["sysctl", "-n", "hw.ncpu"]),
        "host_memory_gib": str(
            round(int(_cmd(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30)
        ),
        "docker_server": str(d.get("ServerVersion", "")),
        "docker_vm_cpus": str(d.get("NCPU", "")),
        "docker_vm_memory_gib": f"{mem_gib:.1f}",
        "docker_os": str(d.get("OperatingSystem", "")),
        "compose": _cmd(["docker", "compose", "version", "--short"]),
        "client_python": platform.python_version(),
        "gateway_python": compose(
            "exec", "-T", "gateway", "python", "--version"
        ).strip(),
        "postgres_image": "postgres:16",
        "redis_image": "redis:7-alpine",
    }
    return info
