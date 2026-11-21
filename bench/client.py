"""Entry point inside the bench-client container: run one scenario, print JSON.

    echo '{"key": "llmgw_..."}' | python -m bench.client overhead

Run by python -m bench (bench/stack.py run_in_network); not meant to be called
by hand. Reads the API keys from stdin and prints exactly one JSON line.
"""

import asyncio
import json
import sys

from bench import scenarios


def main(argv: list[str]) -> int:
    keys = json.loads(sys.stdin.read())
    name = argv[0]
    if name == "overhead":
        coro = scenarios.overhead(keys["key"])
    elif name == "network":
        coro = scenarios.network()
    elif name == "cache":
        coro = scenarios.cache(keys["key"])
    elif name == "single_flight":
        coro = scenarios.single_flight(keys["key"])
    elif name == "resilience_sequential":
        coro = scenarios.resilience_sequential(keys["key"])
    elif name == "resilience_concurrent":
        coro = scenarios.resilience_concurrent(keys["key"])
    elif name == "rate_limit":
        coro = scenarios.rate_limit(keys["rate_limit_key"], int(argv[1]))
    else:
        print(f"unknown scenario {name!r}", file=sys.stderr)
        return 2
    print(json.dumps(asyncio.run(coro)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
