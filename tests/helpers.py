"""Small helpers shared by several test files."""


class FakeClock:
    """A manual clock: time moves only when a test advances it or code sleeps.

    Pass the instance as ``clock`` (it is callable) and ``clock.sleep`` as
    ``sleep``, so retries, backoff and timeouts run instantly but still see
    time pass. Every sleep is recorded in ``sleeps``.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
