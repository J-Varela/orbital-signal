import asyncio
from datetime import UTC, date, datetime

import httpx
import pytest

from orbital_signal.collection import MonthlyCollectionService
from orbital_signal.collection_models import CollectionWindow
from orbital_signal.collection_repository import InMemoryCollectionRepository
from orbital_signal.domain import AwardRecord, CompanySignal
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import build_company_signal
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, AwardSearchPage, USAspendingClient

NASA = DEFAULT_AGENCIES[0]
DAY = date(2026, 9, 1)


async def test_enrichment_disconnect_records_received_rows_and_preserves_existing_signals(
    monkeypatch: pytest.MonkeyPatch, satellite_award: AwardRecord
) -> None:
    fail = True
    transaction_requests = 0

    async def no_delay(_: float) -> None:
        pass

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal transaction_requests
        if request.url.path == "/api/v2/transactions/":
            transaction_requests += 1
            if fail:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(200, json={"results": [{"action_date": "2026-09-01"}]})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "Award ID": "NEW-AWARD",
                        "generated_internal_id": "CONT_AWD_NEW-AWARD",
                        "Recipient Name": "New Satellite Systems LLC",
                        "Award Amount": 500000,
                        "Awarding Agency": NASA,
                        "Description": "Satellite spacecraft payload prototype",
                    }
                ],
                "page_metadata": {"hasNext": False},
            },
        )

    monkeypatch.setattr("orbital_signal.sources.usaspending.asyncio.sleep", no_delay)
    repository = InMemorySignalRepository()
    existing = build_company_signal(satellite_award, as_of=DAY)
    assert existing is not None
    await repository.upsert(satellite_award, existing)
    checkpoints = InMemoryCollectionRepository()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = MonthlyCollectionService(
            source=USAspendingClient(client, base_url="https://source.test"),
            repository=repository,
            checkpoints=checkpoints,
        )
        failed = await service.collect(start_date=DAY, end_date=DAY, agencies=(NASA,))
        assert transaction_requests == 4
        [window] = failed.windows
        assert window.status == "failed"
        assert window.pages_fetched == window.fetched_count == 1
        assert window.stored_count == window.duplicate_count == 0
        assert "transaction_enrichment" in (window.error_message or "")
        assert "HTTP attempts: 4" in (window.error_message or "")
        assert await repository.count() == 1

        fail = False
        resumed = await service.collect(start_date=DAY, end_date=DAY, agencies=(NASA,))
        assert resumed.status == "completed"
        assert resumed.cumulative.attempts == 2
        assert resumed.cumulative.fetched_count == 2
        assert resumed.cumulative.stored_count == 1
        assert resumed.cumulative.failed_attempts == 1
        assert resumed.cumulative.history_complete is True
        assert await repository.count() == 2
        stored = await repository.list()
        assert (
            next(signal for signal in stored if signal.signal_id == existing.signal_id) == existing
        )


async def test_cancelled_collection_retains_uncertain_attempt_and_releases_lock() -> None:
    started = asyncio.Event()

    class Source:
        wait = True

        async def search_awards_page(self, **kwargs) -> AwardSearchPage:
            started.set()
            if self.wait:
                await asyncio.Event().wait()
            return AwardSearchPage([], 0, False)

    source = Source()
    checkpoints = InMemoryCollectionRepository()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )
    task = asyncio.create_task(service.collect(start_date=DAY, end_date=DAY, agencies=(NASA,)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    source.wait = False
    resumed = await service.collect(start_date=DAY, end_date=DAY, agencies=(NASA,))
    assert resumed.status == "completed"
    assert len(resumed.attempts) == 2
    assert resumed.attempts[0].status == "failed"
    assert resumed.attempts[0].counts_complete is False
    assert resumed.cumulative.history_complete is False
    assert all(
        attempt.started_at is not None
        and attempt.completed_at is not None
        and attempt.started_at <= attempt.completed_at
        for attempt in resumed.attempts
    )


async def test_failed_write_counts_relevance_without_claiming_a_duplicate(
    satellite_award: AwardRecord,
) -> None:
    class FailedWrites(InMemorySignalRepository):
        async def upsert(self, award: AwardRecord, signal: CompanySignal) -> bool:
            raise RuntimeError("write failed before persistence")

    class Source:
        async def search_awards_page(self, **kwargs) -> AwardSearchPage:
            return AwardSearchPage([satellite_award], 1, False)

    result = await MonthlyCollectionService(
        source=Source(), repository=FailedWrites(), checkpoints=InMemoryCollectionRepository()
    ).collect(start_date=DAY, end_date=DAY, agencies=(NASA,))
    [window] = result.windows
    assert window.status == "failed"
    assert window.fetched_count == window.relevant_count == 1
    assert window.stored_count == window.duplicate_count == 0
    assert result.cumulative.relevant_count == 1
    assert result.cumulative.history_complete is False


async def test_ambiguous_equally_precise_legacy_overlap_retries_unfinished_coverage() -> None:
    checkpoints = InMemoryCollectionRepository()
    for key, start, end, status in (
        ("completed", DAY, date(2026, 9, 3), "completed"),
        ("failed", date(2026, 9, 2), date(2026, 9, 4), "failed"),
    ):
        await checkpoints.save_window(
            CollectionWindow.model_validate(
                {
                    "window_id": key,
                    "agency": NASA,
                    "month_start": DAY,
                    "start_date": start,
                    "end_date": end,
                    "status": status,
                    "attempts": 1,
                    "started_at": datetime(2026, 10, 1, tzinfo=UTC),
                }
            )
        )

    class Source:
        calls = 0

        async def search_awards_page(self, **kwargs) -> AwardSearchPage:
            self.calls += 1
            assert kwargs["start_date"] == kwargs["end_date"] == date(2026, 9, 2)
            return AwardSearchPage([], 0, False)

    source = Source()
    result = await MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    ).collect(start_date=date(2026, 9, 2), end_date=date(2026, 9, 2), agencies=(NASA,))
    assert source.calls == 1
    assert result.status == "completed"
