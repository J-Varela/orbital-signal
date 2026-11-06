from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import orbital_signal.api as api_module
from orbital_signal.collection import MonthlyCollectionService
from orbital_signal.collection_models import CollectionWindow
from orbital_signal.collection_repository import InMemoryCollectionRepository
from orbital_signal.config import Settings
from orbital_signal.domain import AwardRecord
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import build_company_signal
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, AwardSearchPage

MONTHLY_PATH = "/api/v1/collections/monthly"
NASA = DEFAULT_AGENCIES[0]


class DisconnectedSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date, int]] = []

    async def search_awards_page(
        self,
        *,
        start_date: date,
        end_date: date,
        agency: str,
        page: int = 1,
        records_per_page: int = 100,
    ) -> AwardSearchPage:
        self.calls.append((agency, start_date, end_date, page))
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")


async def test_api_applies_environment_policy_and_stops_without_changing_completed_evidence(
    monkeypatch: pytest.MonkeyPatch, satellite_award: AwardRecord
) -> None:
    for name, value in (
        ("COLLECTION_WINDOW_DELAY_SECONDS", "0"),
        ("COLLECTION_MAX_CONSECUTIVE_FAILURES", "2"),
        ("HTTP_REQUEST_INTERVAL_SECONDS", "1"),
        ("ENRICHMENT_CONCURRENCY", "1"),
    ):
        monkeypatch.setenv(f"ORBITAL_SIGNAL_{name}", value)
    repository = InMemorySignalRepository()
    award = satellite_award.model_copy(
        update={"action_date": date(2026, 9, 2), "awarding_agency": NASA}
    )
    existing_signal = build_company_signal(award, as_of=date(2026, 11, 3))
    assert existing_signal is not None
    await repository.upsert(award, existing_signal)
    checkpoints = InMemoryCollectionRepository()
    started_at = datetime(2026, 10, 1, tzinfo=UTC)
    completed = CollectionWindow(
        window_id="preserved-completed-nasa-window",
        agency=NASA,
        month_start=date(2026, 9, 1),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 3),
        status="completed",
        attempts=1,
        pages_fetched=1,
        fetched_count=1,
        relevant_count=1,
        stored_count=1,
        has_more=False,
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=1),
    )
    await checkpoints.save_window(completed)
    source = DisconnectedSource()
    context = AsyncMock()
    upstream_client = object()
    context.__aenter__.return_value = upstream_client
    http_factory = Mock(return_value=context)
    source_factory = Mock(return_value=source)
    service_factory = Mock(wraps=MonthlyCollectionService)
    monkeypatch.setattr(api_module, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(api_module, "build_http_client", http_factory)
    monkeypatch.setattr(api_module, "build_usaspending_source", source_factory)
    monkeypatch.setattr(api_module, "MonthlyCollectionService", service_factory)
    app = api_module.create_app(repository=repository, collection_repository=checkpoints)
    params = {"start_date": "2026-09-01", "end_date": "2026-09-12", "agency": "nasa"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(MONTHLY_PATH, params=params)
        progress = await client.get(MONTHLY_PATH, params=params)
        signals = await client.get("/api/v1/signals")

    assert response.status_code == progress.status_code == signals.status_code == 200
    result = response.json()
    assert result["status"] == "incomplete"
    assert result["stopped_early"] is True
    assert result["stop_reason"]
    assert result["consecutive_failures"] == result["attempted_windows"] == 2
    assert result["skipped_windows"] == 1
    assert result["window_delay_seconds"] == 0
    assert result["max_consecutive_failures"] == 2
    assert source.calls == [
        (NASA, date(2026, 9, 4), date(2026, 9, 6), 1),
        (NASA, date(2026, 9, 7), date(2026, 9, 9), 1),
    ]
    assert len(result["windows"]) == 3
    assert not any(window["start_date"] >= "2026-09-10" for window in result["windows"])
    assert next(window for window in result["windows"] if window["status"] == "completed") == (
        completed.model_dump(mode="json")
    )
    failed = [window for window in result["windows"] if window["status"] == "failed"]
    assert len(failed) == 2
    assert all("RemoteProtocolError" in window["error_message"] for window in failed)
    assert await repository.list() == [existing_signal]
    assert signals.json()[0]["signal_id"] == existing_signal.signal_id
    assert signals.json()[0]["detected_at"] == existing_signal.detected_at.isoformat().replace(
        "+00:00", "Z"
    )

    persisted = progress.json()
    assert persisted["status"] == "incomplete"
    assert persisted["windows"] == result["windows"]
    assert persisted["stopped_early"] is False
    assert persisted["stop_reason"] is None
    assert persisted["window_delay_seconds"] is None
    assert persisted["max_consecutive_failures"] is None
    assert persisted["consecutive_failures"] == persisted["attempted_windows"] == 0
    settings = app.state.settings
    assert settings.http_request_interval_seconds == settings.enrichment_concurrency == 1
    http_factory.assert_called_once_with(settings)
    source_factory.assert_called_once_with(upstream_client, settings)
    service_factory.assert_called_once_with(
        source=source,
        repository=repository,
        checkpoints=checkpoints,
        window_delay_seconds=0,
        max_consecutive_failures=2,
    )
    context.__aexit__.assert_awaited_once()


async def test_progress_is_read_only_during_collection_and_ignores_enrichment_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "enrichment-cache"
    cache.mkdir()
    marker = cache / "invalid-page.json"
    marker.write_text("This is intentionally not a valid enrichment cache page.")
    before = marker.read_bytes()
    monkeypatch.setenv("ORBITAL_SIGNAL_ENRICHMENT_CACHE_DIRECTORY", str(cache))
    monkeypatch.setattr(api_module, "Settings", lambda: Settings(_env_file=None))
    http_factory = Mock(side_effect=AssertionError("Progress must not create an upstream client"))
    source_factory = Mock(side_effect=AssertionError("Progress must not inspect source caches"))
    monkeypatch.setattr(api_module, "build_http_client", http_factory)
    monkeypatch.setattr(api_module, "build_usaspending_source", source_factory)
    checkpoints = InMemoryCollectionRepository()
    app = api_module.create_app(
        repository=InMemorySignalRepository(), collection_repository=checkpoints
    )

    async with (
        checkpoints.collection_lock(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get(
            MONTHLY_PATH,
            params={"start_month": "2026-09", "end_month": "2026-09", "agency": "nasa"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "incomplete"
    assert response.json()["windows"] == []
    assert response.json()["stopped_early"] is False
    assert list(cache.iterdir()) == [marker]
    assert marker.read_bytes() == before
    http_factory.assert_not_called()
    source_factory.assert_not_called()
