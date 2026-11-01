"""An in-memory stand-in for the Redis client, for tests."""


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int | None] = {}
        self.fail_with: Exception | None = None
        self.gets = 0
        self.sets = 0

    async def get(self, name: str) -> bytes | None:
        self.gets += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.store.get(name)

    async def set(self, name: str, value: str | bytes, ex: int | None = None) -> bool:
        self.sets += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.store[name] = value.encode() if isinstance(value, str) else value
        self.ttls[name] = ex
        return True
