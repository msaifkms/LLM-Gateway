import asyncio

import pytest

from app.core.singleflight import SingleFlight


async def settle() -> None:
    """Let every pending task run until it has to wait."""
    for _ in range(5):
        await asyncio.sleep(0)


class Work:
    """Counts calls and finishes only when the test opens the gate."""

    def __init__(self, result: str = "answer", error: Exception | None = None) -> None:
        self.calls = 0
        self.gate = asyncio.Event()
        self.result = result
        self.error = error

    async def __call__(self) -> str:
        self.calls += 1
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


def test_concurrent_identical_keys_share_one_execution() -> None:
    async def scenario() -> None:
        flight = SingleFlight()
        work = Work()
        tasks = [asyncio.create_task(flight.run("k", work)) for _ in range(10)]
        await settle()
        assert flight.in_flight == 1

        work.gate.set()
        results = await asyncio.gather(*tasks)

        assert work.calls == 1
        assert {value for value, _ in results} == {"answer"}
        assert [leader for _, leader in results].count(True) == 1

    asyncio.run(scenario())


def test_different_keys_run_independently() -> None:
    async def scenario() -> None:
        flight = SingleFlight()
        a, b = Work("a"), Work("b")
        task_a = asyncio.create_task(flight.run("a", a))
        task_b = asyncio.create_task(flight.run("b", b))
        await settle()
        a.gate.set()
        b.gate.set()

        assert (await task_a) == ("a", True)
        assert (await task_b) == ("b", True)
        assert a.calls == b.calls == 1

    asyncio.run(scenario())


def test_finished_work_is_forgotten_so_the_next_call_runs_again() -> None:
    async def scenario() -> None:
        flight = SingleFlight()
        work = Work()
        work.gate.set()

        await flight.run("k", work)
        await settle()
        assert flight.in_flight == 0

        await flight.run("k", work)
        assert work.calls == 2

    asyncio.run(scenario())


def test_errors_are_shared_and_not_retried_per_waiter() -> None:
    async def scenario() -> None:
        flight = SingleFlight()
        work = Work(error=ValueError("provider rejected it"))
        tasks = [asyncio.create_task(flight.run("k", work)) for _ in range(5)]
        await settle()
        work.gate.set()

        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        assert work.calls == 1
        assert all(isinstance(outcome, ValueError) for outcome in outcomes)
        assert flight.in_flight == 0

        # A failure is not remembered: the next call tries again and can succeed.
        healthy = Work("recovered")
        healthy.gate.set()
        assert (await flight.run("k", healthy)) == ("recovered", True)

    asyncio.run(scenario())


def test_cancelling_the_leader_does_not_fail_the_followers() -> None:
    async def scenario() -> None:
        flight = SingleFlight()
        work = Work()
        leader = asyncio.create_task(flight.run("k", work))
        await settle()
        follower = asyncio.create_task(flight.run("k", work))
        await settle()

        leader.cancel()  # e.g. the leader's client disconnected
        await settle()
        work.gate.set()

        assert (await follower) == ("answer", False)
        assert work.calls == 1
        with pytest.raises(asyncio.CancelledError):
            await leader

    asyncio.run(scenario())
