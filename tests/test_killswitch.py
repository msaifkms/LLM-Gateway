import asyncio
import logging

import pytest

from app.core.killswitch import (
    KillSwitch,
    KillSwitchState,
    KillSwitchUnavailable,
)


class FakeFlagStore:
    def __init__(self, enabled: bool = False, delay_s: float = 0) -> None:
        self.enabled = enabled
        self.reason = ""
        self.delay_s = delay_s
        self.error: Exception | None = None
        self.reads = 0

    async def get(self) -> KillSwitchState:
        self.reads += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return KillSwitchState(self.enabled, self.reason)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(store: FakeFlagStore, ttl_s: float = 2.0) -> tuple[KillSwitch, Clock]:
    clock = Clock()
    return KillSwitch(store, ttl_s=ttl_s, clock=clock), clock  # type: ignore[arg-type]


def is_on(switch: KillSwitch) -> bool:
    return asyncio.run(switch.is_on())


def test_it_reports_off_when_the_flag_is_off() -> None:
    switch, _ = make(FakeFlagStore(enabled=False))

    assert is_on(switch) is False


def test_it_reports_on_when_the_flag_is_on() -> None:
    switch, _ = make(FakeFlagStore(enabled=True))

    assert is_on(switch) is True


def test_the_answer_is_cached_so_requests_do_not_each_query_the_database() -> None:
    store = FakeFlagStore()

    async def scenario() -> None:
        switch, clock = make(store)
        for _ in range(50):
            await switch.is_on()
            clock.now += 0.01  # 0.5s in total, still within the 2s cache

    asyncio.run(scenario())

    assert store.reads == 1


def test_a_change_is_noticed_once_the_cache_expires() -> None:
    store = FakeFlagStore(enabled=False)

    async def scenario() -> list[bool]:
        switch, clock = make(store)
        seen = [await switch.is_on()]
        store.enabled = True
        seen.append(await switch.is_on())  # still cached
        clock.now += 2.5
        seen.append(await switch.is_on())  # refreshed
        return seen

    assert asyncio.run(scenario()) == [False, False, True]


def test_simultaneous_requests_share_one_lookup() -> None:
    store = FakeFlagStore(delay_s=0.05)

    async def scenario() -> list[bool]:
        switch, _ = make(store)
        return list(await asyncio.gather(*[switch.is_on() for _ in range(20)]))

    results = asyncio.run(scenario())

    assert results == [False] * 20
    assert store.reads == 1


def test_a_database_error_keeps_the_last_known_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeFlagStore(enabled=True)

    async def scenario() -> bool:
        switch, clock = make(store)
        await switch.is_on()
        store.error = OSError("database went away")
        clock.now += 5
        with caplog.at_level(logging.WARNING):
            return await switch.is_on()

    assert asyncio.run(scenario()) is True  # still paused: do not wake up by accident
    assert "last known value" in caplog.text


def test_with_no_known_value_a_database_error_means_unavailable() -> None:
    store = FakeFlagStore()
    store.error = OSError("database went away")
    switch, _ = make(store)

    with pytest.raises(KillSwitchUnavailable):
        is_on(switch)


def test_after_a_failure_it_retries_on_the_cache_interval_not_every_request() -> None:
    store = FakeFlagStore()
    store.error = OSError("down")

    async def scenario() -> None:
        switch, clock = make(store)
        for _ in range(5):
            with pytest.raises(KillSwitchUnavailable):
                await switch.is_on()
        clock.now += 3
        with pytest.raises(KillSwitchUnavailable):
            await switch.is_on()

    asyncio.run(scenario())

    assert store.reads == 2


def test_turning_it_on_and_off_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeFlagStore(enabled=True)
    store.reason = "provider incident"

    async def scenario() -> None:
        switch, clock = make(store)
        await switch.is_on()
        store.enabled = False
        clock.now += 5
        await switch.is_on()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())

    assert "now ON (provider incident)" in caplog.text
    assert "now OFF" in caplog.text
