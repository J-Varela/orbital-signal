import asyncio
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from orbital_signal import __version__, http_client
from orbital_signal.config import Settings
from orbital_signal.domain import AwardRecord
from orbital_signal.sources import usaspending
from orbital_signal.sources.usaspending import (
    SEARCH_PATH,
    TRANSACTIONS_PATH,
    SourceDataError,
    USAspendingClient,
)


@pytest.fixture
def retry_delays(monkeypatch) -> list[float]:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(usaspending.asyncio, "sleep", sleep)
    monkeypatch.setattr(usaspending.random, "uniform", lambda lower, upper: upper)
    return delays


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.RemoteProtocolError,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
    ],
)
async def test_transient_transport_failures_retry_without_parallel_requests(
    error_type: type[httpx.RequestError], retry_delays: list[float]
) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type("transient transport failure", request=request)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        response = await source._post_with_retry(SEARCH_PATH, json={})

    assert response.status_code == 200
    assert attempts == 2
    assert retry_delays == [1.25]


async def test_transport_retry_exhaustion_preserves_error_type_and_bounded_context(
    retry_delays: list[float],
) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(
            client, base_url="https://api.usaspending.test", retry_attempts=3
        )
        with pytest.raises(httpx.RemoteProtocolError) as caught:
            await source._post_with_retry(SEARCH_PATH, json={})

    assert attempts == 3
    assert retry_delays == [1.25, 2.5]
    assert getattr(caught.value, "retry_attempts", None) == 3
    assert getattr(caught.value, "retry_stop_reason", None) == "attempt_limit"
    assert getattr(caught.value, "source_request_path", None) == SEARCH_PATH
    assert getattr(caught.value, "source_operation", None) == "award_search"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retryable_http_status_is_retried(status: int, retry_delays: list[float]) -> None:
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status if attempts == 1 else 200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        response = await source._post_with_retry(SEARCH_PATH, json={})

    assert response.status_code == 200
    assert attempts == 2
    assert retry_delays == [1.25]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_http_status_is_not_retried(status: int, retry_delays: list[float]) -> None:
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await source._post_with_retry(SEARCH_PATH, json={})

    assert caught.value.response.status_code == status
    assert attempts == 1
    assert retry_delays == []


@pytest.mark.parametrize(
    ("path", "operation"),
    [(SEARCH_PATH, "award_search"), (TRANSACTIONS_PATH, "transaction_enrichment")],
)
async def test_permanent_http_error_records_request_operation_without_claiming_a_received_page(
    path: str, operation: str, retry_delays: list[float]
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await source._post_with_retry(path, json={})

    assert getattr(caught.value, "source_request_path", None) == path
    assert getattr(caught.value, "source_operation", None) == operation
    assert getattr(caught.value, "retry_attempts", None) == 1
    assert getattr(caught.value, "retry_stop_reason", None) == "non_retryable_status"
    assert getattr(caught.value, "source_page_received", False) is False
    assert retry_delays == []


async def test_search_rows_are_reported_when_transaction_enrichment_disconnects(
    retry_delays: list[float],
) -> None:
    search_requests = 0
    transaction_requests = 0
    rows = [
        {
            "Award ID": f"AWARD-{index}",
            "generated_internal_id": f"CONT_AWD_{index}",
            "Recipient Name": "Example Space LLC",
            "Award Amount": 100_000,
            "Awarding Agency": "Department of Defense",
            "Description": "Satellite development.",
        }
        for index in range(2)
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal search_requests, transaction_requests
        if request.url.path == SEARCH_PATH:
            search_requests += 1
            return httpx.Response(200, json={"results": rows, "page_metadata": {"hasNext": True}})
        assert request.url.path == TRANSACTIONS_PATH
        transaction_requests += 1
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(
            client, base_url="https://api.usaspending.test", retry_attempts=2
        )
        with pytest.raises(httpx.RemoteProtocolError) as caught:
            await source.search_awards_page(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 3),
                agency="Department of Defense",
            )

    assert search_requests == 1
    assert transaction_requests >= 2
    assert getattr(caught.value, "source_page_received", False) is True
    assert getattr(caught.value, "source_page_rows", None) == 2
    assert getattr(caught.value, "source_operation", None) == "transaction_enrichment"
    assert getattr(caught.value, "source_request_path", None) == TRANSACTIONS_PATH
    assert getattr(caught.value, "retry_attempts", None) == 2
    assert getattr(caught.value, "retry_stop_reason", None) == "attempt_limit"


@pytest.mark.parametrize("operation", ["page_metadata", "award_mapping"])
async def test_received_search_page_errors_preserve_raw_rows_and_failing_stage(
    operation: str,
) -> None:
    row = {
        "Award ID": "AWARD-1",
        "Recipient Name": "Example Space LLC",
        "Award Amount": 100_000,
        "Awarding Agency": "Department of Defense",
    }
    rows = [row, row | {"Recipient Name": None}] if operation == "award_mapping" else [row, row]
    metadata = {"hasNext": "invalid" if operation == "page_metadata" else False}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"results": rows, "page_metadata": metadata})
        )
    ) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        with pytest.raises(SourceDataError) as caught:
            await source.search_awards_page(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 3),
                agency="Department of Defense",
            )

    assert getattr(caught.value, "source_page_received", False) is True
    assert getattr(caught.value, "source_page_rows", None) == 2
    assert getattr(caught.value, "source_operation", None) == operation
    assert getattr(caught.value, "source_request_path", None) == SEARCH_PATH


@pytest.mark.parametrize(
    "error_type", [httpx.LocalProtocolError, httpx.UnsupportedProtocol, httpx.ProxyError]
)
async def test_client_configuration_errors_are_not_retried(
    error_type: type[httpx.RequestError], retry_delays: list[float]
) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error_type("configuration error", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        with pytest.raises(error_type):
            await source._post_with_retry(SEARCH_PATH, json={})

    assert attempts == 1
    assert retry_delays == []


@pytest.mark.parametrize("date_header", [False, True])
async def test_retry_after_seconds_or_http_date_sets_a_minimum_wait(
    monkeypatch, retry_delays: list[float], date_header: bool
) -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    original_parser = usaspending._retry_after_seconds
    monkeypatch.setattr(
        usaspending, "_retry_after_seconds", lambda value: original_parser(value, now=now)
    )
    header = format_datetime(now + timedelta(seconds=8), usegmt=True) if date_header else "8"
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429 if attempts == 1 else 200,
            headers={"Retry-After": header} if attempts == 1 else {},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        await source._post_with_retry(SEARCH_PATH, json={})

    assert attempts == 2
    assert retry_delays == [8.0]


async def test_retry_after_exceeding_wait_budget_stops_instead_of_retrying_early(
    retry_delays: list[float],
) -> None:
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await source._post_with_retry(SEARCH_PATH, json={})

    assert attempts == 1
    assert retry_delays == []
    assert getattr(caught.value, "retry_attempts", None) == 1
    assert getattr(caught.value, "retry_stop_reason", None) == "retry_after_exceeds_budget"


@pytest.mark.parametrize("header", ["invalid date", "-5", "1.5", ""])
async def test_invalid_retry_after_falls_back_to_bounded_backoff(
    header: str, retry_delays: list[float]
) -> None:
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, headers={"Retry-After": header})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        await source._post_with_retry(SEARCH_PATH, json={})

    assert attempts == 2
    assert retry_delays == [1.25]


async def test_jittered_exponential_backoff_never_exceeds_configured_maximum(
    retry_delays: list[float],
) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadError("upstream read failed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = USAspendingClient(
            client,
            base_url="https://api.usaspending.test",
            retry_attempts=5,
            retry_base_delay_seconds=2,
            retry_max_delay_seconds=3,
        )
        with pytest.raises(httpx.ReadError):
            await source._post_with_retry(SEARCH_PATH, json={})

    assert attempts == 5
    assert retry_delays == [2.5, 3, 3, 3]


@pytest.mark.parametrize("concurrency", [None, 1, 3])
async def test_enrichment_uses_low_default_or_configured_concurrency_and_keeps_order(
    monkeypatch, satellite_award: AwardRecord, concurrency: int | None
) -> None:
    active = maximum = 0

    async def lookup(*, generated_internal_id: str | None):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        try:
            await asyncio.sleep(0.001)
            return satellite_award.start_date
        finally:
            active -= 1

    awards = [
        satellite_award.model_copy(update={"generated_internal_id": f"CONT_AWD_{index}"})
        for index in range(6)
    ]
    options: dict[str, Any] = (
        {"enrichment_concurrency": concurrency} if concurrency is not None else {}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test", **options)
        monkeypatch.setattr(source, "_fetch_latest_action_date", lookup)
        result = await source._enrich_action_dates(awards)

    assert maximum == (concurrency if concurrency is not None else 2)
    assert active == 0
    assert [award.generated_internal_id for award in result] == [
        award.generated_internal_id for award in awards
    ]
    assert all(award.action_date == satellite_award.start_date for award in result)


async def test_enrichment_failure_cancels_and_drains_sibling_requests(
    monkeypatch, satellite_award: AwardRecord
) -> None:
    sibling_started = asyncio.Event()
    never = asyncio.Event()
    active = 0
    drained: list[str | None] = []

    async def lookup(*, generated_internal_id: str | None):
        nonlocal active
        if generated_internal_id == "first":
            await sibling_started.wait()
            raise httpx.RemoteProtocolError("server disconnected")
        active += 1
        sibling_started.set()
        try:
            await never.wait()
        finally:
            await asyncio.sleep(0)
            active -= 1
            drained.append(generated_internal_id)

    awards = [
        satellite_award.model_copy(update={"generated_internal_id": identifier})
        for identifier in ("first", "second", "third", "fourth")
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        monkeypatch.setattr(source, "_fetch_latest_action_date", lookup)
        with pytest.raises(httpx.RemoteProtocolError):
            await source._enrich_action_dates(awards)

    assert active == 0
    assert drained


async def test_cancelling_enrichment_also_drains_workers(
    monkeypatch, satellite_award: AwardRecord
) -> None:
    started = asyncio.Event()
    never = asyncio.Event()
    active = 0

    async def lookup(*, generated_internal_id: str | None):
        nonlocal active
        active += 1
        started.set()
        try:
            await never.wait()
        finally:
            await asyncio.sleep(0)
            active -= 1

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        source = USAspendingClient(client, base_url="https://api.usaspending.test")
        monkeypatch.setattr(source, "_fetch_latest_action_date", lookup)
        task = asyncio.create_task(source._enrich_action_dates([satellite_award] * 4))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert active == 0


async def test_http_helper_preserves_proxy_environment_tls_and_limits(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    original_client = httpx.AsyncClient

    def create_client(**kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(http_client.httpx, "AsyncClient", create_client)
    settings = Settings(
        _env_file=None,
        http_timeout_seconds=12,
        http_max_connections=3,
        http_keepalive_expiry_seconds=2,
    )
    async with http_client.build_http_client(settings) as client:
        assert client.trust_env is True
        assert client.timeout.read == client.timeout.connect == 12
        assert client.timeout.pool == 5
        assert client.headers["User-Agent"] == f"orbital-signal/{__version__}"

    assert captured.get("verify", True) is True
    assert captured["limits"].max_connections == 3
    assert captured["limits"].max_keepalive_connections == 3
    assert captured["limits"].keepalive_expiry == 2


async def test_source_helper_applies_configured_retry_policy(retry_delays: list[float]) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection failed", request=request)

    settings = Settings(
        _env_file=None,
        http_retry_attempts=2,
        http_retry_base_delay_seconds=2,
        http_retry_max_delay_seconds=4,
        http_request_interval_seconds=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = http_client.build_usaspending_source(client, settings)
        with pytest.raises(httpx.ConnectError):
            await source._post_with_retry(SEARCH_PATH, json={})

    assert attempts == 2
    assert retry_delays == [2.5]


def test_http_reliability_settings_load_environment(monkeypatch) -> None:
    monkeypatch.setenv("ORBITAL_SIGNAL_HTTP_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("ORBITAL_SIGNAL_ENRICHMENT_CONCURRENCY", "1")
    monkeypatch.setenv("ORBITAL_SIGNAL_HTTP_MAX_CONNECTIONS", "2")
    monkeypatch.setenv("ORBITAL_SIGNAL_HTTP_KEEPALIVE_EXPIRY_SECONDS", "0")

    settings = Settings(_env_file=None)

    assert settings.http_retry_attempts == 3
    assert settings.enrichment_concurrency == 1
    assert settings.http_max_connections == 2
    assert settings.http_keepalive_expiry_seconds == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"http_retry_attempts": 0},
        {"http_retry_attempts": 11},
        {"enrichment_concurrency": 0},
        {"http_max_connections": 0},
        {"http_retry_base_delay_seconds": -1},
        {"http_retry_max_delay_seconds": 0.5},
        {"http_retry_max_delay_seconds": float("inf")},
        {"http_keepalive_expiry_seconds": -1},
    ],
)
def test_invalid_http_reliability_settings_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
