"""Send a mix of chat requests so the dashboard has something to show.

Usage (gateway running, a key issued with the admin CLI):

    python scripts/traffic.py --key llmgw_... --requests 300 --concurrency 5

The mix is roughly half repeated prompts (cache hits after the first),
a third unique prompts (misses), and the rest at temperature 0.7 (bypass).
"""

import argparse
import asyncio
import random
import uuid
from collections import Counter

import httpx

REPEATED = ["What is a cache?", "Explain retries.", "Define latency.", "Hello!"]


def body(rng: random.Random, model: str) -> dict[str, object]:
    roll = rng.random()
    if roll < 0.5:
        content, temperature = rng.choice(REPEATED), 0.0
    elif roll < 0.83:
        content, temperature = f"unique {uuid.uuid4().hex}", 0.0
    else:
        content, temperature = "Write a haiku.", 0.7
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
    }


async def main(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    statuses: Counter[str] = Counter()
    cache: Counter[str] = Counter()
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    for _ in range(args.requests):
        queue.put_nowait(body(rng, args.model))

    headers = {"Authorization": f"Bearer {args.key}"}
    async with httpx.AsyncClient(base_url=args.url, headers=headers) as client:

        async def worker() -> None:
            while not queue.empty():
                payload = queue.get_nowait()
                try:
                    response = await client.post("/v1/chat/completions", json=payload)
                except httpx.HTTPError as exc:
                    statuses[type(exc).__name__] += 1
                    continue
                statuses[str(response.status_code)] += 1
                cache[response.headers.get("x-cache", "-")] += 1
                if args.delay:
                    await asyncio.sleep(args.delay)

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))

    print("status:", dict(statuses))
    print("x-cache:", dict(cache))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key", required=True, help="API key from issue-key")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--model", default="mock-1")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.05, help="seconds per worker")
    parser.add_argument("--seed", type=int, default=None)
    asyncio.run(main(parser.parse_args()))
