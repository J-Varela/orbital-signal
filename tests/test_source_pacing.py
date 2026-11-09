import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from orbital_signal.config import Settings
from orbital_signal.http_client import build_usaspending_source
from orbital_signal.sources import usaspending
from orbital_signal.sources.usaspending import SEARCH_PATH, TRANSACTIONS_PATH, USAspendingClient


@dataclass
class Clock:
    now: float = 1000
    sleeps: list[float] = field(default_factory=list)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    original_sleep = asyncio.sleep

    async def sleep(delay: float) -> None:
        clock.sleeps.append(delay)
        clock.now += delay
        await original_sleep(0)

    monkeypatch.setattr(usaspending, "monotonic", lambda: clock.now)
    monkeypatch.setattr(usaspending.asyncio, "sleep", sleep)
    monkeypatch.setattr(usaspending.random, "uniform", lambda lower, upper: 0)
    return clock


async def test_shared_client_spaces_search_enrichment_and_other_source_wrappers(
    clock: Clock,
) -> None:
    starts: list[float] = []

    def handle(_: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        first = USAspendingClient(
            client, base_url="https://api.usaspending.test", request_interval_seconds=0.5
        )
        second = USAspendingClient(
            client, base_url="https://api.usaspending.test", request_interval_seconds=0.5
        )
        await asyncio.gather(
            first._post_with_retry(SEARCH_PATH, json={}),
            first._post_with_retry(TRANSACTIONS_PATH, json={}),
            second._post_with_retry(SEARCH_PATH, json={}),
        )

    assert starts == [1000, 1000.5, 1001]
    assert clock.sleeps == [0.5, 0.5]


async def test_pacing_applies_to_retry_attempts_in_addition_to_short_backoff(clock: Clock) -> None:
    starts: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        if len(starts) == 1:
            raise httpx.RemoteProtocolError("disconnect", request=request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(
            client,
            base_url="https://api.usaspending.test",
            request_interval_seconds=1,
            retry_base_delay_seconds=0.1,
            retry_max_delay_seconds=1,
        )
        await source._post_with_retry(SEARCH_PATH, json={})

    assert starts == pytest.approx([1000, 1001])
    assert sum(clock.sleeps) == pytest.approx(1)
    assert clock.sleeps[0] == pytest.approx(0.1)


async def test_retry_after_is_not_shortened_by_request_pacing(clock: Clock) -> None:
    starts: list[float] = []

    def handle(_: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        if len(starts) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(
            client, base_url="https://api.usaspending.test", request_interval_seconds=0.5
        )
        await source._post_with_retry(SEARCH_PATH, json={})

    assert starts == [1000, 1002]
    assert clock.sleeps == [2]


async def test_zero_interval_preserves_no_pacing_for_direct_sources(clock: Clock) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        await source._post_with_retry(SEARCH_PATH, json={})
        await source._post_with_retry(TRANSACTIONS_PATH, json={})

    assert clock.sleeps == []


async def test_factory_uses_configured_default_request_interval(clock: Clock) -> None:
    starts: list[float] = []

    def handle(_: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = build_usaspending_source(client, Settings(_env_file=None))
        await source._post_with_retry(SEARCH_PATH, json={})
        await source._post_with_retry(TRANSACTIONS_PATH, json={})

    assert starts == [1000, 1000.5]


@pytest.mark.parametrize("interval", [-1, float("inf"), float("nan")])
async def test_invalid_source_request_interval_is_rejected(interval: float) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            USAspendingClient(
                client, base_url="https://api.usaspending.test", request_interval_seconds=interval
            )


@pytest.mark.parametrize(
    "values",
    [
        {"http_request_interval_seconds": -1},
        {"http_request_interval_seconds": float("nan")},
        {"collection_window_delay_seconds": -1},
        {"collection_window_delay_seconds": float("inf")},
        {"collection_max_consecutive_failures": 0},
        {"enrichment_cache_ttl_seconds": 0},
        {"enrichment_cache_ttl_seconds": float("inf")},
        {"enrichment_cache_max_pages": 0},
    ],
)
def test_invalid_collection_and_cache_policy_is_rejected(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
