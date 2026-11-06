from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise

import httpx
import pytest

from orbital_signal.collection import MonthlyCollectionService, month_windows
from orbital_signal.collection_repository import InMemoryCollectionRepository
from orbital_signal.domain import AwardRecord, CompanySignal
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, AwardSearchPage

NASA, DOD = DEFAULT_AGENCIES
JANUARY = date(2026, 1, 1)
JANUARY_END = date(2026, 1, 31)


@dataclass(frozen=True)
class PageRequest:
    agency: str
    start_date: date
    end_date: date
    page: int


class StubPageSource:
    def __init__(self, handler: Callable[[PageRequest], AwardSearchPage]) -> None:
        self.handler = handler
        self.requests: list[PageRequest] = []

    async def search_awards_page(
        self,
        *,
        start_date: date,
        end_date: date,
        agency: str,
        page: int = 1,
        records_per_page: int = 100,
    ) -> AwardSearchPage:
        assert 1 <= records_per_page <= 100
        request = PageRequest(agency, start_date, end_date, page)
        self.requests.append(request)
        return self.handler(request)


def empty_page() -> AwardSearchPage:
    return AwardSearchPage(awards=[], fetched_count=0, has_more=False)


def page_with(award: AwardRecord, *, has_more: bool = False) -> AwardSearchPage:
    return AwardSearchPage(awards=[award], fetched_count=1, has_more=has_more)


def another_award(award: AwardRecord, source_id: str) -> AwardRecord:
    return award.model_copy(
        update={
            "source_award_id": source_id,
            "generated_internal_id": f"CONT_AWD_{source_id}",
        }
    )


def test_initial_backfill_covers_january_through_october_as_calendar_months() -> None:
    windows = month_windows(JANUARY, date(2026, 10, 1))

    assert len(windows) == 10
    assert windows[0] == (JANUARY, JANUARY_END)
    assert windows[1] == (date(2026, 2, 1), date(2026, 2, 28))
    assert windows[-1] == (date(2026, 10, 1), date(2026, 10, 31))
    assert all(
        end + timedelta(days=1) == next_start for (_, end), (next_start, _) in pairwise(windows)
    )


@pytest.mark.parametrize(
    ("start_month", "end_month", "expected"),
    [
        (
            date(2024, 2, 1),
            date(2024, 2, 1),
            [(date(2024, 2, 1), date(2024, 2, 29))],
        ),
        (
            date(2025, 12, 1),
            JANUARY,
            [(date(2025, 12, 1), date(2025, 12, 31)), (JANUARY, JANUARY_END)],
        ),
    ],
)
def test_month_boundaries_handle_leap_years_and_year_rollover(
    start_month: date,
    end_month: date,
    expected: list[tuple[date, date]],
) -> None:
    assert month_windows(start_month, end_month) == expected


@pytest.mark.parametrize(
    ("start_month", "end_month"),
    [
        (date(2026, 2, 1), JANUARY),
        (date(2026, 1, 2), date(2026, 2, 1)),
        (JANUARY, date(2026, 2, 28)),
    ],
)
def test_invalid_month_selection_is_rejected(start_month: date, end_month: date) -> None:
    with pytest.raises(ValueError):
        month_windows(start_month, end_month)


async def test_historical_collection_searches_both_agencies_for_every_selected_month() -> None:
    source = StubPageSource(lambda _: empty_page())
    checkpoints = InMemoryCollectionRepository()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )

    result = await service.collect(window_days=31, start_month=JANUARY, end_month=date(2026, 10, 1))

    expected = {
        PageRequest(agency, start, end, 1)
        for start, end in month_windows(JANUARY, date(2026, 10, 1))
        for agency in DEFAULT_AGENCIES
    }
    assert set(source.requests) == expected
    assert len(source.requests) == 20
    assert result.status == "completed"
    assert len(result.windows) == 20
    assert all(window.status == "completed" for window in result.windows)
    assert all(window.has_more is False for window in result.windows)


async def test_pages_advance_until_source_reports_no_next_page(
    satellite_award: AwardRecord,
) -> None:
    second_award = another_award(satellite_award, "SECOND")

    def respond(request: PageRequest) -> AwardSearchPage:
        if request.agency == DOD:
            return empty_page()
        if request.page == 1:
            return page_with(satellite_award, has_more=True)
        assert request.page == 2
        return page_with(second_award)

    source = StubPageSource(respond)
    repository = InMemorySignalRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=InMemoryCollectionRepository()
    )

    result = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    nasa = next(window for window in result.windows if window.agency == NASA)
    assert nasa.status == "completed"
    assert nasa.pages_fetched == 2
    assert nasa.fetched_count == nasa.relevant_count == nasa.stored_count == 2
    assert nasa.duplicate_count == 0
    assert nasa.has_more is False
    assert await repository.count() == 2
    assert [request.page for request in source.requests if request.agency == NASA] == [1, 2]


async def test_busy_month_splits_into_nonoverlapping_exhausted_windows(
    satellite_award: AwardRecord,
) -> None:
    def respond(request: PageRequest) -> AwardSearchPage:
        if request.agency == NASA and (request.start_date, request.end_date) == (
            JANUARY,
            JANUARY_END,
        ):
            return page_with(satellite_award, has_more=True)
        return empty_page()

    source = StubPageSource(respond)
    service = MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
        page_limit=1,
    )

    result = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    nasa = [window for window in result.windows if window.agency == NASA]
    [parent] = [window for window in nasa if window.status == "split"]
    leaves = sorted(
        (window for window in nasa if window.status != "split"),
        key=lambda window: window.start_date,
    )
    assert (parent.start_date, parent.end_date) == (JANUARY, JANUARY_END)
    assert parent.has_more is True
    assert len(leaves) == 2
    assert leaves[0].start_date == JANUARY
    assert leaves[0].end_date + timedelta(days=1) == leaves[1].start_date
    assert leaves[1].end_date == JANUARY_END
    assert all(window.status == "completed" and window.has_more is False for window in leaves)
    assert result.status == "completed"


async def test_single_day_page_cap_remains_incomplete(satellite_award: AwardRecord) -> None:
    busy_day = date(2026, 1, 15)

    def respond(request: PageRequest) -> AwardSearchPage:
        if request.agency == NASA and request.start_date <= busy_day <= request.end_date:
            return page_with(satellite_award, has_more=True)
        return empty_page()

    source = StubPageSource(respond)
    repository = InMemorySignalRepository()
    service = MonthlyCollectionService(
        source=source,
        repository=repository,
        checkpoints=InMemoryCollectionRepository(),
        page_limit=1,
    )

    result = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    [incomplete] = [window for window in result.windows if window.status == "incomplete"]
    assert incomplete.start_date == incomplete.end_date == busy_day
    assert incomplete.has_more is True
    assert incomplete.pages_fetched == 1
    assert result.status == "incomplete"
    assert await repository.count() == 1


async def test_partial_source_failure_keeps_progress_and_resumes_from_first_page(
    satellite_award: AwardRecord,
) -> None:
    fail_second_page = True
    second_award = another_award(satellite_award, "AFTER-RETRY")

    def respond(request: PageRequest) -> AwardSearchPage:
        if request.agency == DOD:
            return empty_page()
        if request.page == 1:
            return page_with(satellite_award, has_more=True)
        if fail_second_page:
            raise httpx.ReadTimeout("second page unavailable")
        return page_with(second_award)

    source = StubPageSource(respond)
    repository = InMemorySignalRepository()
    checkpoints = InMemoryCollectionRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=checkpoints
    )

    first = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)
    [original_signal] = await repository.list()
    failed = next(window for window in first.windows if window.agency == NASA)
    assert first.status == "incomplete"
    assert failed.status == "failed"
    assert failed.pages_fetched == failed.fetched_count == failed.stored_count == 1
    assert "second page unavailable" in (failed.error_message or "")
    assert any(window.agency == DOD and window.status == "completed" for window in first.windows)

    source.requests.clear()
    fail_second_page = False
    resumed = await MonthlyCollectionService(
        source=source, repository=repository, checkpoints=checkpoints
    ).collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    assert source.requests == [
        PageRequest(NASA, JANUARY, JANUARY_END, 1),
        PageRequest(NASA, JANUARY, JANUARY_END, 2),
    ]
    retried = next(window for window in resumed.windows if window.agency == NASA)
    assert resumed.status == "completed"
    assert resumed.skipped_windows >= 1
    assert retried.attempts == 2
    assert retried.status == "completed"
    assert retried.error_message is None
    assert retried.pages_fetched == retried.fetched_count == retried.relevant_count == 2
    assert retried.stored_count == retried.duplicate_count == 1
    assert await repository.count() == 2
    signals = await repository.list()
    assert (
        next(
            signal.detected_at
            for signal in signals
            if signal.signal_id == original_signal.signal_id
        )
        == original_signal.detected_at
    )

    source.requests.clear()
    repeated = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)
    assert source.requests == []
    assert repeated.skipped_windows == 2


async def test_resume_retries_only_failed_split_child(satellite_award: AwardRecord) -> None:
    failure_enabled = True
    failed_range: tuple[date, date] | None = None

    def respond(request: PageRequest) -> AwardSearchPage:
        nonlocal failed_range
        if request.agency == DOD:
            return empty_page()
        if (request.start_date, request.end_date) == (JANUARY, JANUARY_END):
            return page_with(satellite_award, has_more=True)
        if request.end_date == JANUARY_END and failure_enabled:
            failed_range = (request.start_date, request.end_date)
            raise httpx.ConnectError("child request failed")
        return empty_page()

    source = StubPageSource(respond)
    repository = InMemorySignalRepository()
    checkpoints = InMemoryCollectionRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=checkpoints, page_limit=1
    )
    first = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)
    assert first.status == "incomplete"
    assert failed_range is not None

    failure_enabled = False
    source.requests.clear()
    second = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    assert source.requests == [PageRequest(NASA, *failed_range, 1)]
    assert second.status == "completed"
    assert second.skipped_windows >= 2


async def test_refresh_revisits_finished_windows_without_duplicate_signals(
    satellite_award: AwardRecord,
) -> None:
    source = StubPageSource(
        lambda request: page_with(satellite_award) if request.agency == NASA else empty_page()
    )
    repository = InMemorySignalRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=InMemoryCollectionRepository()
    )
    await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)
    [original_signal] = await repository.list()
    source.requests.clear()

    refreshed = await service.collect(
        window_days=31, start_month=JANUARY, end_month=JANUARY, refresh=True
    )

    assert len(source.requests) == 2
    assert refreshed.skipped_windows == 0
    nasa = next(window for window in refreshed.windows if window.agency == NASA)
    assert nasa.attempts == 2
    assert nasa.stored_count == 0
    assert nasa.duplicate_count == 1
    [signal] = await repository.list()
    assert signal.signal_id == original_signal.signal_id
    assert signal.detected_at == original_signal.detected_at


async def test_interrupted_running_window_is_retried(satellite_award: AwardRecord) -> None:
    source = StubPageSource(
        lambda request: page_with(satellite_award) if request.agency == NASA else empty_page()
    )
    repository = InMemorySignalRepository()
    checkpoints = InMemoryCollectionRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=checkpoints
    )
    first = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)
    nasa = next(window for window in first.windows if window.agency == NASA)
    await checkpoints.save_window(
        nasa.model_copy(update={"status": "running", "completed_at": None})
    )
    source.requests.clear()

    resumed = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    assert source.requests == [PageRequest(NASA, JANUARY, JANUARY_END, 1)]
    nasa = next(window for window in resumed.windows if window.agency == NASA)
    assert nasa.status == "completed"
    assert nasa.attempts == 2
    assert nasa.stored_count == 0
    assert nasa.duplicate_count == 1


async def test_partial_storage_failure_records_committed_awards_and_can_resume(
    satellite_award: AwardRecord,
) -> None:
    second_award = another_award(satellite_award, "STORAGE-RETRY")

    class FailingRepository(InMemorySignalRepository):
        fail_second_award = True

        async def upsert(self, award: AwardRecord, signal: CompanySignal) -> bool:
            if self.fail_second_award and award.source_award_id == second_award.source_award_id:
                raise RuntimeError("test storage failure")
            return await super().upsert(award, signal)

    source = StubPageSource(
        lambda request: (
            AwardSearchPage(awards=[satellite_award, second_award], fetched_count=2, has_more=False)
            if request.agency == NASA
            else empty_page()
        )
    )
    repository = FailingRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=InMemoryCollectionRepository()
    )

    first = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    failed = next(window for window in first.windows if window.agency == NASA)
    assert first.status == "incomplete"
    assert failed.status == "failed"
    assert failed.fetched_count == 2
    assert failed.stored_count == 1
    assert "test storage failure" in (failed.error_message or "")
    assert await repository.count() == 1

    repository.fail_second_award = False
    resumed = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    nasa = next(window for window in resumed.windows if window.agency == NASA)
    assert resumed.status == "completed"
    assert nasa.stored_count == nasa.duplicate_count == 1
    assert await repository.count() == 2


async def test_split_checkpoint_without_children_is_incomplete_and_resumable() -> None:
    source = StubPageSource(lambda _: empty_page())
    repository = InMemorySignalRepository()
    checkpoints = InMemoryCollectionRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=checkpoints
    )
    first = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)
    nasa = next(window for window in first.windows if window.agency == NASA)
    await checkpoints.save_window(nasa.model_copy(update={"status": "split", "has_more": True}))
    source.requests.clear()

    progress = await service.progress(start_month=JANUARY, end_month=JANUARY)

    assert progress.status == "incomplete"

    resumed = await service.collect(window_days=31, start_month=JANUARY, end_month=JANUARY)

    assert resumed.status == "completed"
    assert len(source.requests) == 2
    assert all(request.agency == NASA for request in source.requests)
    assert all(
        (request.start_date, request.end_date) != (JANUARY, JANUARY_END)
        for request in source.requests
    )
